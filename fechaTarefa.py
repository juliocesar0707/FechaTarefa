#!/usr/bin/env python3
"""Automacao para fechar tarefas no Tasky (maxdatafortaleza.com.br).

Fluxo em 3 passos:
  1) python fechaTarefa.py login       -> pede e-mail e senha, loga e salva a sessao em sessao.json
  2) python fechaTarefa.py analisar    -> le as tarefas DE QUEM LOGOU e gera relatorio.csv com a tag sugerida
  3) python fechaTarefa.py aplicar     -> aplica a tag e conclui SO as linhas marcadas no relatorio

Nada e alterado no Tasky antes do passo 3.

Outro fluxo:
  python fechaTarefa.py cancelar      -> cancela as tarefas 'Sem responsavel' com o motivo 'sem tarefa'

Manutencao (nao abrem navegador):
  python fechaTarefa.py verificar      -> valida config.json + regras.json + tags_tasky.json
  python fechaTarefa.py testar         -> roda os casos de testes.json contra as regras

Fluxo de escrita em cada tarefa (conferido na tela real):
  lapis 'Editar tarefa' -> botao '+ Tag' -> busca -> clique na tag -> 'Salvar' -> 'Concluir'
"""
from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

try:
    from playwright.sync_api import Error as PWError
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright
    TEM_PLAYWRIGHT = True
except ImportError:                      # 'verificar' e 'testar' rodam sem navegador
    class PWError(Exception):
        pass

    class PWTimeout(PWError):
        pass

    sync_playwright = None
    TEM_PLAYWRIGHT = False

RAIZ = Path(__file__).resolve().parent
ARQ_CONFIG = RAIZ / "config.json"
ARQ_REGRAS = RAIZ / "regras.json"
ARQ_TAGS = RAIZ / "tags_tasky.json"
ARQ_CRED = RAIZ / "credenciais.json"
ARQ_SESSAO = RAIZ / "sessao.json"
ARQ_RELATORIO = RAIZ / "relatorio.csv"
ARQ_LOG = RAIZ / "log_aplicacao.csv"
ARQ_TESTES = RAIZ / "testes.json"
DIR_ERROS = RAIZ / "erros"

MARCAS_APROVACAO = {"x", "s", "sim", "1", "true", "ok"}
COLUNAS = [
    "aprovado", "id", "titulo", "departamento", "coluna", "url", "tag_sugerida",
    "confianca", "regra", "termo", "trecho", "alternativas", "tags_atuais", "observacao",
]
COLUNAS_LOG = ["quando", "id", "url", "tag", "status", "detalhe"]

ALTA, MEDIA, BAIXA = "alta", "media", "baixa"
FORCA = {ALTA: 3, MEDIA: 2, BAIXA: 1}
SEPARADOR = " || "          # junta as mensagens sem deixar um termo casar entre duas delas

# Ruido removido do historico ANTES de casar as regras. Sem isso a assinatura do
# tecnico (financeiro@maxdatafortaleza.com.br) marcava a tarefa como financeira.
RUIDO_PADRAO = [
    r"\S+@\S+",                              # e-mails
    r"https?://\S+",
    r"www\.\S+",
    r"pro-\d{4}/\d+",                        # protocolo do TendiChat
    r"\(?\d{2}\)?\s*9?\d{4}[-\s]?\d{4}",     # telefone
    r"\*[^*]{2,40}\*",                       # marcador de autor: *julio cesar*
    r"\[tendichat\]",
]


# --------------------------------------------------------------------------- util

def sair(msg: str, codigo: int = 1):
    print(f"ERRO: {msg}", file=sys.stderr)
    raise SystemExit(codigo)


def carregar_json(caminho: Path) -> dict:
    if not caminho.exists():
        sair(f"Arquivo nao encontrado: {caminho}")
    try:
        with caminho.open(encoding="utf-8") as fp:
            return json.load(fp)
    except json.JSONDecodeError as exc:
        sair(f"{caminho.name}: JSON invalido na linha {exc.lineno}, coluna {exc.colno} - {exc.msg}")


def normalizar(texto: str) -> str:
    """Minusculas, sem acentos e com espacos colapsados."""
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto).strip().lower()


def escrever_csv(caminho: Path, colunas: list[str], linhas: list[dict], acrescentar: bool = False):
    """Grava o CSV inteiro por arquivo temporario, para nao corromper o original."""
    if acrescentar and caminho.exists():
        with caminho.open("a", newline="", encoding="utf-8-sig") as fp:
            csv.DictWriter(fp, fieldnames=colunas, delimiter=";",
                           extrasaction="ignore", restval="").writerows(linhas)
        return
    tmp = caminho.with_suffix(caminho.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as fp:
        w = csv.DictWriter(fp, fieldnames=colunas, delimiter=";",
                           extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(linhas)
    tmp.replace(caminho)


# ------------------------------------------------------------------------ regras

_CACHE_TERMOS: dict[str, re.Pattern] = {}


def compilar_termo(termo: str) -> re.Pattern:
    """Transforma um termo de regras.json em regex.

      'boleto'   -> palavra inteira ('dicarnes' nao casa com 'carne')
      'agend*'   -> prefixo de palavra ('agendar', 'agendamento')
      're:...'   -> regex escrita a mao, usada como esta

    A busca por pedaco solto (find()) foi trocada por isto porque 'cte' casava
    dentro de 'conectem' e 'nfe' dentro de 'infelizmente' - tag errada na tarefa.
    """
    padrao = _CACHE_TERMOS.get(termo)
    if padrao is None:
        if termo.startswith("re:"):
            padrao = re.compile(termo[3:])
        elif termo.endswith("*"):
            padrao = re.compile(r"(?<![0-9a-z])" + re.escape(normalizar(termo[:-1])))
        else:
            padrao = re.compile(r"(?<![0-9a-z])" + re.escape(normalizar(termo)) + r"(?![0-9a-z])")
        _CACHE_TERMOS[termo] = padrao
    return padrao


def compilar_ruido(cfg: dict) -> list[re.Pattern]:
    return [re.compile(p) for p in cfg.get("limpar_texto", RUIDO_PADRAO)]


def preparar_blocos(blocos: list[str], ruido: list[re.Pattern]) -> list[str]:
    """Normaliza e tira o ruido de cada mensagem do historico."""
    prontos = []
    for bloco in blocos:
        alvo = normalizar(bloco)
        for padrao in ruido:
            alvo = padrao.sub(" ", alvo)
        alvo = re.sub(r"\s+", " ", alvo).strip()
        if alvo:
            prontos.append(alvo)
    return prontos


def _achar_termo(alvo: str, termo: str):
    """-> (trecho em volta, texto que casou) ou None."""
    m = compilar_termo(termo).search(alvo)
    if not m:
        return None
    ini, fim = m.span()
    return alvo[max(0, ini - 45): fim + 45].strip(), m.group(0)


@dataclass
class Candidato:
    tag: str
    regra: str
    prioridade: int
    trecho: str
    termos: list[str] = field(default_factory=list)   # termos da regra que casaram
    casou: str = ""                                   # o texto que disparou


@dataclass
class Veredito:
    tag: str = ""
    regra: str = ""
    trecho: str = ""
    termo: str = ""
    confianca: str = ""
    motivo: str = ""
    alternativas: str = ""


def avaliar_regras(alvos: list[str], regras: list[dict], departamento: str = "") -> list[Candidato]:
    """Todas as regras que batem, da mais forte para a mais fraca.

    'qualquer': basta um termo aparecer em qualquer mensagem do historico.
    'todas': os termos precisam estar na MESMA mensagem - sem isso, palavras
    soltas em pontos distantes da conversa casavam por acidente.
    """
    alvos = [a for a in alvos if a]
    if not alvos:
        return []
    inteiro = SEPARADOR.join(alvos)

    achados: list[Candidato] = []
    for regra in regras:
        termos = regra.get("palavras", [])
        prioridade = regra.get("prioridade", 0)
        # a mesma regra pode render tags diferentes conforme o departamento da tarefa
        # (ex.: financeiro no dep. Financeiro = cobranca da Maxdata; no Suporte = modulo do sistema)
        tag = regra.get("tag_por_departamento", {}).get(departamento, regra["tag"])
        nome = regra.get("nome", regra["tag"])

        if regra.get("modo", "qualquer") == "todas":
            for alvo in alvos:
                pares = [(t, _achar_termo(alvo, t)) for t in termos]
                if pares and all(r for _, r in pares):
                    achados.append(Candidato(tag, nome, prioridade, pares[0][1][0],
                                             [t for t, _ in pares], pares[0][1][1]))
                    break
        else:
            batidos, trecho, casou = [], "", ""
            for termo in termos:
                achado = _achar_termo(inteiro, termo)
                if achado:
                    batidos.append(termo)
                    if not trecho:
                        trecho, casou = achado
            if batidos:
                achados.append(Candidato(tag, nome, prioridade, trecho, batidos, casou))

    achados.sort(key=lambda c: (-c.prioridade, -len(c.termos)))
    return achados


def julgar(achados: list[Candidato], regras: list[dict], margem: int = 8) -> Veredito:
    """Escolhe a tag e diz o quanto da para confiar nela.

      baixa -> so termo generico, ou outra tag empatada na disputa
      media -> tem concorrente distante, ou regra guarda-chuva (prioridade baixa)
      alta  -> ganhou sozinha e com termo especifico

    So a confianca a partir do nivel configurado em 'aprovar_automatico' entra
    pre-marcada com 'x' no relatorio. O resto espera decisao humana.
    """
    if not achados:
        return Veredito()
    vencedor = achados[0]
    por_nome = {r.get("nome", r["tag"]): r for r in regras}
    regra_vencedora = por_nome.get(vencedor.regra, {})
    fracas = {normalizar(t) for t in regra_vencedora.get("palavras_fracas", [])}
    so_fraco = bool(fracas) and all(normalizar(t) in fracas for t in vencedor.termos)
    # disputas ja decididas de proposito (ex.: boleto vencido e 'Boleto Atualizado',
    # nao 'financeiro Maxdata') nao devem rebaixar a confianca toda vez
    resolvidas = set(regra_vencedora.get("disputa_ok", []))

    rivais = [c for c in achados[1:] if c.tag != vencedor.tag]
    empatados = [c for c in rivais if vencedor.prioridade - c.prioridade <= margem
                 and c.regra not in resolvidas]

    if so_fraco:
        conf, motivo = BAIXA, f"so termo generico ('{vencedor.casou}')"
    elif empatados:
        conf, motivo = BAIXA, f"disputa com '{empatados[0].tag}' (prioridade parecida)"
    elif vencedor.prioridade < 25:
        conf, motivo = MEDIA, "regra guarda-chuva (prioridade baixa)"
    elif rivais and len(vencedor.termos) == 1:
        conf, motivo = MEDIA, f"um unico termo e ha outro assunto na conversa ('{rivais[0].tag}')"
    else:
        conf, motivo = ALTA, ""

    alternativas = "; ".join(f"{c.tag} [{c.prioridade}] por '{c.casou}'" for c in rivais[:3])
    return Veredito(vencedor.tag, vencedor.regra, vencedor.trecho, vencedor.casou,
                    conf, motivo, alternativas)


def tags_oficiais() -> list[str]:
    return list(carregar_json(ARQ_TAGS)["tags"].values()) if ARQ_TAGS.exists() else []


def tag_por_nota(alvos: list[str], oficiais: list[str]):
    """Nota curta do tecnico com o nome exato de uma tag.

    E uma decisao manual escrita na propria tarefa - vale mais que as regras de
    palavra-chave. Ex.: uma atualizacao so com 'inatividade' ou 'Sem tarefa'.
    """
    for alvo in reversed(alvos):            # da atualizacao mais recente para tras
        if not alvo or len(alvo) > 40:
            continue
        for tag in oficiais:
            if normalizar(tag) == alvo:
                return tag, alvo
    return None


def classificar(blocos: list[str], regras: list[dict], oficiais: list[str],
                ruido: list[re.Pattern], departamento: str, margem: int = 8) -> Veredito:
    """Decide a tag de uma tarefa: nota do tecnico primeiro, depois as regras."""
    alvos = preparar_blocos(blocos, ruido)
    nota = tag_por_nota(alvos, oficiais)
    if nota:
        return Veredito(nota[0], "nota do tecnico", nota[1], nota[1], ALTA)
    v = julgar(avaliar_regras(alvos, regras, departamento), regras, margem)
    if not v.tag and alvos:
        # sem regra que bata, o 'trecho' vinha vazio e nao dava para saber o que
        # a conversa dizia: aqui vai uma amostra dela, para escrever a regra que falta
        v.trecho = SEPARADOR.join(alvos)
    return v


# ----------------------------------------------------------------------- validacao

def validar_projeto(cfg: dict) -> list[tuple[str, str]]:
    """Confere config/regras/tags antes de qualquer coisa. -> [(ERRO|AVISO, texto)]"""
    problemas: list[tuple[str, str]] = []
    erro = lambda m: problemas.append(("ERRO", m))
    aviso = lambda m: problemas.append(("AVISO", m))

    for chave in ("url_login", "url_tarefas", "seletores"):
        if not cfg.get(chave):
            erro(f"config.json sem '{chave}'")
    nivel = str(cfg.get("aprovar_automatico", ALTA)).lower()
    if nivel not in FORCA and nivel != "nunca":
        erro(f"config.json: 'aprovar_automatico' deve ser alta/media/baixa/nunca (esta '{nivel}')")
    for padrao in cfg.get("limpar_texto", RUIDO_PADRAO) + cfg.get("ignorar_blocos", []):
        alvo = padrao[3:] if padrao.startswith("re:") else padrao
        try:
            re.compile(alvo)
        except re.error as exc:
            erro(f"config.json: regex invalida '{padrao}' - {exc}")

    regras = carregar_json(ARQ_REGRAS).get("regras", [])
    oficiais = {normalizar(t) for t in tags_oficiais()}
    nomes: dict[str, int] = {}
    dono_termo: dict[str, list[tuple[str, str, int]]] = {}

    for regra in regras:
        nome = regra.get("nome", regra.get("tag", "(sem nome)"))
        if not regra.get("tag"):
            erro(f"regra '{nome}' sem 'tag'")
            continue
        if not regra.get("palavras"):
            erro(f"regra '{nome}' sem 'palavras'")
        if regra.get("modo", "qualquer") not in ("qualquer", "todas"):
            erro(f"regra '{nome}': 'modo' deve ser 'qualquer' ou 'todas'")
        nomes[nome] = nomes.get(nome, 0) + 1

        tags_regra = [regra["tag"]] + list(regra.get("tag_por_departamento", {}).values())
        for tag in tags_regra:
            if oficiais and normalizar(tag) not in oficiais:
                erro(f"regra '{nome}' aponta para tag inexistente no Tasky: '{tag}'")

        for termo in regra.get("palavras", []):
            try:
                compilar_termo(termo)
            except re.error as exc:
                erro(f"regra '{nome}': termo invalido '{termo}' - {exc}")
                continue
            if not termo.startswith("re:") and len(termo.rstrip("*")) < 3:
                aviso(f"regra '{nome}': termo '{termo}' tem menos de 3 letras - risco de casar demais")
            if termo != termo.strip():
                aviso(f"regra '{nome}': termo '{termo}' tem espaco sobrando nas pontas")
            chave = normalizar(termo)
            dono_termo.setdefault(chave, []).append((nome, regra["tag"], regra.get("prioridade", 0)))

        for fraca in regra.get("palavras_fracas", []):
            if fraca not in regra.get("palavras", []):
                aviso(f"regra '{nome}': 'palavras_fracas' cita '{fraca}', que nao esta em 'palavras'")

    conhecidas = {r.get("nome", r.get("tag", "")) for r in regras}
    for regra in regras:
        nome = regra.get("nome", regra.get("tag", ""))
        for outra in regra.get("disputa_ok", []):
            if outra not in conhecidas:
                erro(f"regra '{nome}': 'disputa_ok' cita a regra '{outra}', que nao existe")

    for nome, quantas in nomes.items():
        if quantas > 1:
            erro(f"nome de regra repetido: '{nome}' ({quantas}x) - o relatorio fica ambiguo")

    for termo, donos in dono_termo.items():
        tags = {d[1] for d in donos}
        prioridades = {d[2] for d in donos}
        if len(tags) > 1 and len(prioridades) < len(donos):
            quem = ", ".join(f"{d[0]} [{d[2]}]" for d in donos)
            aviso(f"termo '{termo}' esta em regras empatadas com tags diferentes: {quem}")

    return problemas


def exigir_projeto_valido(cfg: dict):
    problemas = validar_projeto(cfg)
    for nivel, texto in problemas:
        print(f"{nivel}: {texto}")
    if any(n == "ERRO" for n, _ in problemas):
        sair("configuracao invalida - corrija os ERRO acima antes de rodar.")
    if problemas:
        print()


# ---------------------------------------------------------------------- navegador

def exigir_playwright():
    if not TEM_PLAYWRIGHT:
        sair("Playwright nao instalado. Rode:\n"
             "  pip install playwright\n"
             "  python -m playwright install chromium")


def abrir_contexto(pw, cfg: dict, headless: bool, slow_mo: int | None = None,
                   sessao: bool = True):
    """sessao=False abre o navegador limpo - o 'login' nao herda os cookies de quem logou antes."""
    nav = pw.chromium.launch(headless=headless,
                             slow_mo=cfg.get("slow_mo_ms", 0) if slow_mo is None else slow_mo)
    ctx = nav.new_context(
        storage_state=str(ARQ_SESSAO) if sessao and ARQ_SESSAO.exists() else None,
        viewport={"width": 1500, "height": 950},
    )
    ctx.set_default_timeout(cfg.get("timeout_ms", 20000))
    return nav, ctx


def sel(cfg: dict, chave: str) -> list[str]:
    valor = cfg["seletores"].get(chave) or []
    return [valor] if isinstance(valor, str) else list(valor)


def localizar(escopo, seletores: list[str], timeout: int = 3000):
    """Tenta cada seletor da lista e devolve o primeiro elemento visivel."""
    for s in seletores:
        try:
            loc = escopo.locator(s).first
            loc.wait_for(state="visible", timeout=timeout)
            return loc
        except (PWTimeout, PWError):
            continue
    return None


def texto_de(escopo, seletores: list[str]) -> str:
    """Junta o texto dos elementos do primeiro seletor da lista que existir."""
    for s in seletores:
        try:
            loc = escopo.locator(s)
            if loc.count() == 0:
                continue
            partes = [t.strip() for t in loc.all_inner_texts() if t and t.strip()]
            if partes:
                return "\n".join(partes)
        except PWError:
            continue
    return ""


def conferir_sessao(page):
    """Sessao expirada faz o Tasky devolver a tela de login em vez da tarefa."""
    if "/login" in page.url:
        sair("A sessao expirou (o Tasky voltou para /login). "
             "Rode de novo: python fechaTarefa.py login")


def usuario_logado(ctx, cfg: dict) -> dict:
    """Quem esta logado, segundo a sessao do proprio Tasky: {'id', 'nome', 'email', ...}.

    O 'id' e o mesmo value do filtro de tecnicos (Julio Cesar = 3, Elson Jr = 2), entao
    da para filtrar a fila de quem fez o login sem nada fixo no config.json. {} = sem sessao.
    """
    url = cfg.get("url_sessao") or base_de(cfg["url_tarefas"]) + "/api/auth/session"
    try:
        resp = ctx.request.get(url)
        dados = resp.json() if resp.ok else None
    except (PWError, ValueError):
        return {}
    return (dados or {}).get("user") or {}


def ler_historico(page, cfg: dict) -> tuple[list[str], int]:
    """Mensagens do historico sem as automaticas do bot. -> (blocos, descartados)"""
    blocos = []
    for s in sel(cfg, "historico"):
        loc = page.locator(s)
        if loc.count():
            blocos = [t.strip() for t in loc.all_inner_texts() if t and t.strip()]
            break

    padroes = []
    for p in cfg.get("ignorar_blocos", []):
        padroes.append(re.compile(p[3:]) if p.startswith("re:") else re.compile(re.escape(normalizar(p))))

    uteis, descartados = [], 0
    for bloco in blocos:
        alvo = normalizar(bloco)
        if any(p.search(alvo) for p in padroes):
            descartados += 1
            continue
        uteis.append(bloco)
    return uteis, descartados


def id_da_url(url: str) -> str:
    m = re.search(r"/tarefas/(\d+)", url)
    return m.group(1) if m else url.rsplit("/", 1)[-1][:40]


def base_de(url: str) -> str:
    partes = url.split("/")
    return f"{partes[0]}//{partes[2]}"


def absolutizar(base_url: str, href: str) -> str:
    if href.startswith("http"):
        return href
    return base_de(base_url) + (href if href.startswith("/") else "/" + href)


# ------------------------------------------------------------------- lista/filtro

def aplicar_filtro_tecnico(page, cfg: dict):
    """Filtra a fila pelo tecnico e CONFERE quem ficou selecionado.

    'filtro_tecnico': 'logado' usa o id e o nome de quem fez o login - cada analista
    ve a propria fila na propria maquina. Um value fixo ('3') continua valendo para
    olhar a fila de outra pessoa.

    O nome e comparado INTEIRO com o texto que ficou no combo: se o Tasky renumerar
    os tecnicos, o robo para em vez de fechar a fila de quem nao devia. Comparar por
    'contem' nao serve - 'Elson' esta dentro de 'Elson Jr', e sao duas pessoas.
    """
    valor = str(cfg.get("filtro_tecnico") or "")
    nome = str(cfg.get("filtro_tecnico_nome") or "")
    if valor == "logado":
        usuario = usuario_logado(page.context, cfg)
        if not usuario.get("id"):
            sair("Nao consegui saber quem esta logado no Tasky (sessao expirada?). "
                 "Rode de novo: python fechaTarefa.py login")
        valor, nome = str(usuario["id"]), str(usuario.get("nome") or "")
    if not valor and not nome:
        return
    combo = localizar(page, sel(cfg, "filtro_tecnico"), timeout=8000)
    if not combo:
        sair("Filtro de tecnicos nao encontrado. Rode 'inspecionar' e ajuste o seletor - "
             "seguir sem filtro mexeria na fila de todo mundo.")
    try:
        combo.select_option(valor) if valor else combo.select_option(label=nome)
    except (PWTimeout, PWError):
        sair(f"Nao consegui selecionar o tecnico ({valor or nome}) no filtro. "
             "Confira 'filtro_tecnico'/'filtro_tecnico_nome' no config.json.")
    page.wait_for_timeout(2500)

    escolhido = combo.evaluate("el => el.selectedOptions.length ? el.selectedOptions[0].text : ''")
    if nome and normalizar(nome) != normalizar(escolhido):
        sair(f"O filtro selecionou '{escolhido.strip()}', mas era esperado "
             f"'{nome}'. Os ids do Tasky provavelmente mudaram - ajuste 'filtro_tecnico'.")
    origem = " (usuario logado)" if cfg.get("filtro_tecnico") == "logado" else ""
    print(f"Filtro de tecnico aplicado: {escolhido.strip() or valor}{origem}")


def escopos(page, cfg: dict, colunas: list[str]):
    """Escopos de busca: cada coluna pedida do Kanban, ou a pagina inteira."""
    if not colunas:
        return [("pagina", page)]
    molde = sel(cfg, "coluna")[0]
    achados = []
    for nome in colunas:
        loc = page.locator(molde.replace("{coluna}", nome))
        if loc.count() == 0:
            print(f"AVISO: coluna '{nome}' nao encontrada.")
            continue
        achados.append((nome, loc.first))
    if not achados:
        sair("Nenhuma das colunas configuradas foi encontrada no Kanban.")
    return achados


def coletar_tarefas(page, cfg: dict, limite: int, colunas: list[str]) -> list[dict]:
    page.goto(cfg["url_tarefas"], wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    conferir_sessao(page)
    aplicar_filtro_tecnico(page, cfg)

    filtro_texto = cfg.get("filtro_texto_card") or ""
    tarefas, vistos = [], set()
    for nome_coluna, escopo in escopos(page, cfg, colunas):
        cards = escopo.locator(sel(cfg, "lista_tarefas")[0])
        if filtro_texto:
            cards = cards.filter(has_text=filtro_texto)
        total = cards.count()
        print(f"[{nome_coluna}] {total} card(s)" + (f" contendo '{filtro_texto}'" if filtro_texto else ""))

        for i in range(total):
            card = cards.nth(i)
            href = None
            for s in sel(cfg, "link_tarefa"):
                try:
                    link = card.locator(s).first
                    if link.count():
                        href = link.get_attribute("href")
                        if href:
                            break
                except PWError:
                    continue
            if not href:
                continue
            url = absolutizar(page.url, href)
            if url in vistos:
                continue
            vistos.add(url)
            tarefas.append({
                "url": url,
                "coluna": nome_coluna,
                "resumo": (card.inner_text() or "").strip().replace("\n", " ")[:120],
            })
            if limite and len(tarefas) >= limite:
                return tarefas
    return tarefas


# ---------------------------------------------------------------------- comandos

def terminal_interativo() -> bool:
    """Ha alguem no teclado? No Windows o dispositivo NUL tambem passa por isatty(),
    entao confere se e um console de verdade."""
    if not sys.stdin or not sys.stdin.isatty():
        return False
    if os.name != "nt":
        return True
    import ctypes
    import msvcrt
    modo = ctypes.c_uint32()
    return bool(ctypes.windll.kernel32.GetConsoleMode(
        msvcrt.get_osfhandle(sys.stdin.fileno()), ctypes.byref(modo)))


def credenciais() -> tuple[str, str, str]:
    """-> (email, senha, origem).

    No terminal o e-mail e a senha sao SEMPRE perguntados: cada analista entra com o
    proprio login, e uma pasta copiada de outra maquina nao loga com a conta de quem
    a copiou. TASKY_EMAIL/TASKY_SENHA pulam a pergunta; o credenciais.json so e lido
    quando nao ha terminal para perguntar (execucao automatica).
    """
    email = os.environ.get("TASKY_EMAIL", "")
    senha = os.environ.get("TASKY_SENHA", "")
    if email and senha:
        return email, senha, "TASKY_EMAIL/TASKY_SENHA"
    if terminal_interativo():
        try:
            email = input(f"E-mail do Tasky{f' [{email}]' if email else ''}: ").strip() or email
            senha = getpass.getpass("Senha (nao aparece enquanto digita): ")
            return email, senha, "digitado"
        except EOFError:
            pass
    if ARQ_CRED.exists():
        dados = carregar_json(ARQ_CRED)
        return email or dados.get("email", ""), dados.get("senha", ""), ARQ_CRED.name
    return email, senha, ""


def abrir_tela_login(page, cfg: dict):
    """Espera a tela de login ficar utilizavel - recarrega uma vez antes de desistir.

    - O CDN do Tasky as vezes entrega o /login guardado no formato interno do Next.js
      (text/x-component) em vez da pagina: tela crua, sem campo de e-mail. Um parametro
      unico na URL passa por fora desse cache (12/12 contra 7/12 sem ele, em 13/10/2026).
    - O campo aparece antes do JavaScript do Tasky assumir o formulario; um 'Entrar'
      clicado nesse intervalo so recarrega o /login, e parecia senha errada.
    """
    sep = "&" if "?" in cfg["url_login"] else "?"
    for _ in range(3):
        page.goto(f"{cfg['url_login']}{sep}t={datetime.now():%Y%m%d%H%M%S%f}",
                  wait_until="domcontentloaded")
        try:
            page.wait_for_selector("input[type='email']", state="visible", timeout=10000)
        except PWTimeout:
            continue
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except PWTimeout:
            pass
        page.wait_for_timeout(500)
        return
    sair("A tela de login do Tasky nao mostrou o campo de e-mail. "
         "Tente de novo ou use: python fechaTarefa.py login --manual")


def cmd_login(cfg: dict, args):
    exigir_playwright()
    tentativas = 3 if terminal_interativo() else 1

    with sync_playwright() as pw:
        # navegador limpo: o login de um analista nao pode herdar a sessao do anterior
        nav, ctx = abrir_contexto(pw, cfg, headless=not args.manual, sessao=False)
        page = ctx.new_page()

        if args.manual:
            abrir_tela_login(page, cfg)
            print("\nFaca o login no navegador que abriu (inclusive 2FA, se houver).")
            input("Quando a tela de tarefas aparecer, volte aqui e tecle ENTER... ")
        else:
            for tentativa in range(1, tentativas + 1):
                email, senha, origem = credenciais()
                if not email or not senha:
                    nav.close()
                    sair("Sem e-mail/senha. Rode 'python fechaTarefa.py login' num terminal "
                         "para digitar, ou defina TASKY_EMAIL e TASKY_SENHA.")
                abrir_tela_login(page, cfg)
                page.fill("input[type='email']", email)
                page.fill("input[type='password']", senha)
                page.click("button[type='submit']")
                try:
                    page.wait_for_url(lambda u: "/login" not in u, timeout=15000)
                    break
                except PWTimeout:
                    print(f"Login recusado para {email} - e-mail ou senha incorretos.")
                    if origem != "digitado" or tentativa == tentativas:
                        nav.close()
                        sair(f"Login falhou (credenciais de: {origem}). Confira o e-mail e a senha "
                             "ou use: python fechaTarefa.py login --manual")

        usuario = usuario_logado(ctx, cfg)
        if not usuario.get("id"):
            nav.close()
            sair("O Tasky nao reconheceu a sessao - o login nao foi concluido. Rode 'login' de novo.")
        ctx.storage_state(path=str(ARQ_SESSAO))
        nav.close()
    print(f"Login OK: {usuario.get('nome')} ({usuario.get('email')})")
    if str(cfg.get("filtro_tecnico") or "") == "logado":
        print(f"O 'analisar' vai trazer a fila de {usuario.get('nome')}.")
    print(f"Sessao salva em {ARQ_SESSAO} (contem cookies: nao compartilhe).")


def cmd_analisar(cfg: dict, args):
    exigir_playwright()
    exigir_projeto_valido(cfg)
    regras = carregar_json(ARQ_REGRAS)["regras"]
    oficiais = tags_oficiais()
    ruido = compilar_ruido(cfg)
    margem = int(cfg.get("margem_disputa", 8))
    minimo = str(cfg.get("aprovar_automatico", ALTA)).lower()
    if not ARQ_SESSAO.exists():
        sair("Sessao ausente. Rode primeiro: python fechaTarefa.py login")

    colunas = args.coluna if args.coluna else cfg.get("colunas", [])
    with sync_playwright() as pw:
        nav, ctx = abrir_contexto(pw, cfg, headless=args.headless)
        page = ctx.new_page()
        tarefas = coletar_tarefas(page, cfg, args.limite, colunas)
        print(f"{len(tarefas)} tarefa(s) para analisar.\n")

        linhas = []
        for n, tarefa in enumerate(tarefas, 1):
            page.goto(tarefa["url"], wait_until="domcontentloaded")
            page.wait_for_timeout(cfg.get("pausa_entre_tarefas_ms", 1200))
            conferir_sessao(page)

            tit = localizar(page, sel(cfg, "titulo_tarefa"), timeout=4000)
            titulo = tit.inner_text().strip() if tit else tarefa["resumo"]
            blocos, descartados = ler_historico(page, cfg)
            tags_atuais = texto_de(page, sel(cfg, "tags_ja_aplicadas")).replace("\n", " | ")
            dep = texto_de(page, sel(cfg, "departamento")).split("\n")[0].strip()

            v = classificar(blocos, regras, oficiais, ruido, dep, margem)
            if not blocos:
                obs = "historico vazio (so mensagens automaticas?)"
            elif not v.tag:
                obs = "nenhuma regra bateu"
            else:
                obs = f"conferir: {v.motivo}" if v.motivo else ""

            aprovar = (bool(v.tag) and minimo != "nunca"
                       and FORCA.get(v.confianca, 0) >= FORCA.get(minimo, 3))
            linhas.append({
                "aprovado": "x" if aprovar else "",
                "id": id_da_url(tarefa["url"]),
                "titulo": titulo.replace("\n", " ")[:120],
                "departamento": dep,
                "coluna": tarefa.get("coluna", ""),
                "url": tarefa["url"],
                "tag_sugerida": v.tag,
                "confianca": v.confianca,
                "regra": v.regra,
                "termo": v.termo,
                "trecho": v.trecho[:200],
                "alternativas": v.alternativas[:200],
                "tags_atuais": tags_atuais[:120],
                "observacao": obs,
            })
            marca = "x" if aprovar else " "
            print(f"[{n}/{len(tarefas)}] [{marca}] #{linhas[-1]['id']}: "
                  f"{v.tag or '(sem tag)'}"
                  f"{f' ({v.confianca})' if v.tag else ''}"
                  f"{(' - ' + obs) if obs else ''}"
                  f"{f'  [{descartados} msg automatica(s) ignorada(s)]' if descartados else ''}")

        nav.close()

    escrever_csv(ARQ_RELATORIO, COLUNAS, linhas)

    com_tag = sum(1 for l in linhas if l["tag_sugerida"])
    marcadas = sum(1 for l in linhas if l["aprovado"])
    print(f"\nRelatorio: {ARQ_RELATORIO}")
    print(f"{com_tag} de {len(linhas)} tarefa(s) com tag sugerida; "
          f"{marcadas} pre-marcada(s) com 'x' (confianca minima: {minimo}).")
    if com_tag > marcadas:
        print(f"{com_tag - marcadas} sugestao(oes) ficaram SEM 'x' de proposito - "
              "confira a coluna 'observacao' e marque a mao o que estiver certo.")
    print("Revise o CSV e rode: python fechaTarefa.py aplicar")


def chips_da_tarefa(page, cfg: dict) -> list[str]:
    for s in sel(cfg, "tags_ja_aplicadas"):
        loc = page.locator(s)
        if loc.count():
            return [t.strip() for t in loc.all_inner_texts() if t and t.strip()]
    return []


def aplicar_tag(page, cfg: dict, tag: str, simular: bool = False) -> str:
    """lapis -> +Tag -> busca -> clique na tag -> Salvar. '' = sucesso."""
    if any(normalizar(c) == normalizar(tag) for c in chips_da_tarefa(page, cfg)):
        print(f"      (tag '{tag}' ja estava aplicada, pulando a edicao)")
        return ""

    editar = localizar(page, sel(cfg, "botao_editar"), timeout=6000)
    if not editar:
        return "botao 'Editar tarefa' (lapis) nao encontrado"
    editar.click()
    page.wait_for_timeout(900)

    btn_tag = localizar(page, sel(cfg, "abrir_campo_tag"), timeout=5000)
    if not btn_tag:
        return "botao '+ Tag' nao encontrado"
    btn_tag.click()
    page.wait_for_timeout(700)

    busca = localizar(page, sel(cfg, "busca_tag"), timeout=5000)
    if not busca:
        return "campo 'Buscar tag...' nao encontrado"
    busca.fill(tag)
    page.wait_for_timeout(800)

    # o clique so pode acontecer dentro do popover: procurar na pagina inteira
    # arriscaria clicar em outro botao com o mesmo texto.
    popover = localizar(page, sel(cfg, "popover_tag"), timeout=3000)
    if not popover:
        return "popover da lista de tags nao apareceu"
    opcao = popover.get_by_role("button", name=tag, exact=True)
    if opcao.count() == 0:
        return f"tag '{tag}' nao apareceu na lista"
    if opcao.count() > 1:
        return f"a busca por '{tag}' devolveu {opcao.count()} opcoes iguais - nao da para escolher sozinho"
    opcao.first.click()
    page.wait_for_timeout(700)

    # o popover fica por cima do botao Salvar e o Esc nao o fecha:
    # clicar de novo no '+ Tag' fecha o popover mantendo a tag escolhida.
    if page.locator(sel(cfg, "popover_tag")[0]).count():
        btn_tag.click()
        page.wait_for_timeout(700)

    chips = chips_da_tarefa(page, cfg)
    if not any(normalizar(c) == normalizar(tag) for c in chips):
        return f"a tag '{tag}' nao entrou na tarefa (chips: {chips})"

    if simular:
        return "SIMULACAO: pararia aqui, sem clicar em Salvar"

    salvar = localizar(page, sel(cfg, "botao_salvar"), timeout=5000)
    if not salvar:
        return "botao 'Salvar' nao encontrado"
    salvar.click()
    page.wait_for_timeout(1500)

    # confere no que ficou gravado, nao no que estava na tela do editor
    page.reload(wait_until="domcontentloaded")
    page.wait_for_timeout(cfg.get("pausa_entre_tarefas_ms", 1200))
    gravadas = chips_da_tarefa(page, cfg)
    if not any(normalizar(c) == normalizar(tag) for c in gravadas):
        return f"clicou em Salvar mas a tag '{tag}' nao ficou gravada (chips: {gravadas})"
    return ""


def concluir_tarefa(page, cfg: dict, aviso: dict, simular: bool = False) -> str:
    """Clica em Concluir e trata o confirm() do Tasky. '' = sucesso."""
    aviso["mensagem"] = ""
    fechar = localizar(page, sel(cfg, "botao_fechar"), timeout=6000)
    if not fechar:
        return "botao 'Concluir' nao encontrado"
    if simular:
        return "SIMULACAO: nao clicou em Concluir"
    fechar.click()
    page.wait_for_timeout(1200)

    confirmar = localizar(page, sel(cfg, "botao_confirmar"), timeout=2000)
    if confirmar:
        confirmar.click()
    page.wait_for_timeout(1800)

    ainda = page.locator(sel(cfg, "botao_fechar")[0])
    if ainda.count() and ainda.first.is_visible():
        if aviso["mensagem"]:
            return f"nao concluida - {aviso['mensagem']}"
        return "clicou em Concluir mas o botao continua na tela - confira manualmente"
    return ""


def cmd_aplicar(cfg: dict, args):
    exigir_playwright()
    exigir_projeto_valido(cfg)
    if not ARQ_RELATORIO.exists():
        sair(f"{ARQ_RELATORIO} nao existe. Rode antes: python fechaTarefa.py analisar")

    nascimento = ARQ_RELATORIO.stat().st_mtime
    for arq in (ARQ_REGRAS, ARQ_CONFIG, ARQ_TAGS):
        if arq.exists() and arq.stat().st_mtime > nascimento:
            print(f"AVISO: {arq.name} foi alterado depois do relatorio.csv. "
                  "As sugestoes podem estar desatualizadas - considere rodar 'analisar' de novo.\n")

    with ARQ_RELATORIO.open(encoding="utf-8-sig") as fp:
        leitor = csv.DictReader(fp, delimiter=";")
        if not leitor.fieldnames or "tag_sugerida" not in leitor.fieldnames:
            sair("relatorio.csv sem a coluna 'tag_sugerida' - "
                 "o Excel pode ter salvo com outro separador. Gere de novo com 'analisar'.")
        todas = [{c: (l.get(c) or "").strip() for c in COLUNAS} for l in leitor]

    fila = [l for l in todas
            if l["aprovado"].lower() in MARCAS_APROVACAO and l["tag_sugerida"]]
    if args.limite:
        fila = fila[:args.limite]
    if not fila:
        sair("Nenhuma linha aprovada no relatorio (coluna 'aprovado' com 'x' e tag preenchida).")

    # tag digitada errado na mao nao pode virar clique perdido na tela do Tasky
    oficiais = {normalizar(t): t for t in tags_oficiais()}
    if oficiais:
        ruins = sorted({l["tag_sugerida"] for l in fila if normalizar(l["tag_sugerida"]) not in oficiais})
        if ruins:
            sair("O relatorio tem tag que nao existe no Tasky: " + ", ".join(ruins) +
                 "\nCorrija a coluna 'tag_sugerida' (veja tags_tasky.json) e rode de novo.")
        for l in fila:                      # acerta maiusculas/acentos do nome oficial
            l["tag_sugerida"] = oficiais[normalizar(l["tag_sugerida"])]

    sem_conferencia = {l["id"] for l in fila if l["confianca"] and l["confianca"] != ALTA}
    print(f"{len(fila)} tarefa(s) aprovadas:")
    for l in fila[:20]:
        alerta = f"  <- confianca {l['confianca']}" if l["id"] in sem_conferencia else ""
        print(f"   #{l['id']} -> {l['tag_sugerida']}  ({l['titulo'][:50]}){alerta}")
    if len(fila) > 20:
        print(f"   ... e mais {len(fila) - 20}")
    if sem_conferencia:
        print(f"\nATENCAO: {len(sem_conferencia)} linha(s) aprovadas a mao com confianca abaixo de 'alta'.")

    if args.simular:
        print("\nModo simulacao: vai abrir cada tarefa e conferir os botoes, sem salvar nem concluir.")
    elif not args.sim:
        resp = input("\nConfirma aplicar a tag e CONCLUIR essas tarefas no Tasky? [s/N] ").strip().lower()
        if resp not in {"s", "sim", "y"}:
            print("Cancelado. Nada foi alterado.")
            return

    DIR_ERROS.mkdir(exist_ok=True)
    resultados = []
    with sync_playwright() as pw:
        nav, ctx = abrir_contexto(pw, cfg, headless=args.headless, slow_mo=args.devagar)
        page = ctx.new_page()
        usuario = usuario_logado(ctx, cfg)
        if not usuario.get("id"):
            nav.close()
            sair("A sessao expirou. Rode de novo: python fechaTarefa.py login")
        print(f"Aplicando como: {usuario.get('nome')} ({usuario.get('email')})")

        # O Tasky usa confirm() nativo para avisar de mensagem nao lida no TendiChat.
        #   "concluir_com_aviso": true  -> clica OK, mas SO nos avisos listados em
        #                                  'avisos_aceitos' (o resto continua recusado)
        #   --forcar                    -> clica OK em qualquer aviso
        # Um confirm() desconhecido pode ser outra coisa (apagar, descartar), por isso
        # aceitar tudo so acontece quando voce pede na linha de comando.
        aceita_conhecidos = bool(cfg.get("concluir_com_aviso"))
        conhecidos = [re.compile(normalizar(p)) for p in cfg.get("avisos_aceitos", [])]
        aviso = {"mensagem": "", "aceito": False}

        def tratar_dialogo(d):
            texto = " ".join(d.message.split())
            aviso["mensagem"] = texto[:150]
            combina = any(p.search(normalizar(texto)) for p in conhecidos)
            if args.forcar or (aceita_conhecidos and combina):
                aviso["aceito"] = True
                d.accept()
            else:
                d.dismiss()

        page.on("dialog", tratar_dialogo)

        for n, item in enumerate(fila, 1):
            status, detalhe = "ok", ""
            aviso["mensagem"], aviso["aceito"] = "", False
            try:
                page.goto(item["url"], wait_until="domcontentloaded")
                page.wait_for_timeout(cfg.get("pausa_entre_tarefas_ms", 1200))
                conferir_sessao(page)
                # garante que estamos na tarefa da linha do CSV, e nao numa
                # redirecionada pelo Tasky
                if id_da_url(page.url) != item["id"]:
                    raise RuntimeError(f"abriu a tarefa #{id_da_url(page.url)} em vez de #{item['id']}")

                erro = aplicar_tag(page, cfg, item["tag_sugerida"], args.simular)
                if erro and not erro.startswith("SIMULACAO"):
                    raise RuntimeError(erro)
                if erro:
                    status, detalhe = "simulado", erro
                else:
                    erro = concluir_tarefa(page, cfg, aviso, args.simular)
                    if erro and erro.startswith("SIMULACAO"):
                        status, detalhe = "simulado", erro
                    elif erro:
                        # a tag JA esta gravada; so a conclusao falhou
                        status, detalhe = "tag_aplicada", erro
                    elif aviso["aceito"]:
                        # fica registrado no log que essa foi fechada por cima de um aviso
                        detalhe = f"aviso aceito: {aviso['mensagem']}"
            except (PWTimeout, PWError, RuntimeError) as exc:
                status = "falha"
                detalhe = str(exc).splitlines()[0][:180]
                try:
                    page.screenshot(path=str(DIR_ERROS / f"{item['id']}.png"), full_page=True)
                except PWError:
                    pass

            resultados.append({
                "quando": datetime.now().isoformat(timespec="seconds"),
                "id": item["id"], "url": item["url"],
                "tag": item["tag_sugerida"], "status": status, "detalhe": detalhe,
            })
            print(f"[{n}/{len(fila)}] #{item['id']} -> {status} {detalhe}")

        nav.close()

    if args.simular:
        ok = sum(1 for r in resultados if r["status"] != "falha")
        print(f"\nSimulacao: {ok} de {len(resultados)} passariam. Nada foi gravado no Tasky.")
        return

    escrever_csv(ARQ_LOG, COLUNAS_LOG, resultados, acrescentar=True)

    # marca no relatorio o que ja foi aplicado, para nao repetir numa proxima rodada
    quando = f"{datetime.now():%d/%m/%Y %H:%M}"
    por_id = {r["id"]: r for r in resultados}
    for linha in todas:
        r = por_id.get(linha["id"])
        if not r:
            continue
        if r["status"] == "ok":
            linha["aprovado"] = "feito"
            linha["observacao"] = f"concluida em {quando}"
        elif r["status"] == "tag_aplicada":
            # o 'x' fica: numa proxima rodada a tag e pulada e so a conclusao e tentada
            linha["observacao"] = f"TAG JA APLICADA em {quando}, falta concluir: {r['detalhe'][:90]}"
    escrever_csv(ARQ_RELATORIO, COLUNAS, todas)

    ok = sum(1 for r in resultados if r["status"] == "ok")
    parados = [r for r in resultados if r["status"] == "tag_aplicada"]
    falhas = [r for r in resultados if r["status"] == "falha"]
    print(f"\n{ok} concluida(s), {len(parados)} com tag aplicada mas sem concluir, "
          f"{len(falhas)} com falha.")
    print(f"Log: {ARQ_LOG}")

    if parados:
        print(f"\n{len(parados)} tarefa(s) ficaram com a TAG JA GRAVADA, so faltando concluir "
              "(o Tasky barrou). Nao precisa refazer a tag:")
        for r in parados:
            print(f"   #{r['id']}: {r['detalhe'][:100]}")
        print("   -> resolva no TendiChat e rode 'aplicar' de novo (a tag e pulada), "
              "ou use --forcar para concluir mesmo assim.")

    aceitas = [r for r in resultados if r["status"] == "ok" and r["detalhe"].startswith("aviso aceito")]
    if aceitas:
        print(f"\n{len(aceitas)} tarefa(s) foram concluidas POR CIMA de um aviso do Tasky "
              "(esta anotado no log):")
        for r in aceitas:
            print(f"   #{r['id']}: {r['detalhe'][14:110]}")
    if falhas:
        print(f"\nScreenshots das {len(falhas)} falha(s) em: {DIR_ERROS}")


def cancelar_tarefa(page, cfg: dict, motivo: str, simular: bool = False) -> str:
    """Cancelar -> digita o motivo no modal -> Confirmar cancelamento. '' = sucesso."""
    # a fila foi lida antes: se alguem assumiu a tarefa nesse meio tempo, nao mexe
    if not localizar(page, sel(cfg, "sem_responsavel"), timeout=3000):
        return "a tarefa ganhou responsavel - nao foi cancelada"

    btn = localizar(page, sel(cfg, "botao_cancelar"), timeout=6000)
    if not btn:
        return "botao 'Cancelar' nao encontrado"
    btn.click()
    page.wait_for_timeout(800)

    modal = localizar(page, sel(cfg, "modal_cancelar"), timeout=4000)
    if not modal:
        return "o modal de cancelamento nao apareceu"
    campo = localizar(modal, sel(cfg, "motivo_cancelar"), timeout=3000)
    if not campo:
        return "campo do motivo nao encontrado no modal"
    campo.fill(motivo)
    page.wait_for_timeout(400)

    confirmar = localizar(modal, sel(cfg, "confirmar_cancelar"), timeout=3000)
    if not confirmar:
        return "botao 'Confirmar cancelamento' nao encontrado"
    if not confirmar.is_enabled():
        return "'Confirmar cancelamento' continua desabilitado depois de digitar o motivo"
    if simular:
        modal.get_by_role("button", name="Voltar", exact=True).click()
        return "SIMULACAO: motivo digitado, nao clicou em Confirmar cancelamento"
    confirmar.click()
    page.wait_for_timeout(1500)

    # confere no que ficou gravado: tarefa cancelada nao mostra mais o botao
    page.reload(wait_until="domcontentloaded")
    page.wait_for_timeout(cfg.get("pausa_entre_tarefas_ms", 1200))
    ainda = page.locator(sel(cfg, "botao_cancelar")[0])
    if ainda.count() and ainda.first.is_visible():
        return "clicou em Confirmar cancelamento mas o botao Cancelar continua na tela - confira manualmente"
    return ""


def cmd_cancelar(cfg: dict, args):
    """Cancela as tarefas 'Sem responsavel' com o motivo do config (padrao: 'sem tarefa')."""
    exigir_playwright()
    if not ARQ_SESSAO.exists():
        sair("Sessao ausente. Rode primeiro: python fechaTarefa.py login")
    opc = cfg.get("cancelar", {})
    motivo = args.motivo or opc.get("motivo") or "sem tarefa"
    # mesma lista do 'analisar', mas filtrada por 'Sem responsavel' em vez do tecnico
    cfg_fila = dict(cfg, filtro_tecnico=opc.get("filtro_tecnico", "sem"),
                    filtro_tecnico_nome=opc.get("filtro_tecnico_nome", "Sem responsável"))
    colunas = args.coluna or []

    DIR_ERROS.mkdir(exist_ok=True)
    resultados = []
    with sync_playwright() as pw:
        nav, ctx = abrir_contexto(pw, cfg, headless=args.headless, slow_mo=args.devagar)
        page = ctx.new_page()
        fila = coletar_tarefas(page, cfg_fila, args.limite, colunas)
        if not fila:
            nav.close()
            print("Nenhuma tarefa sem responsavel encontrada.")
            return

        print(f"\n{len(fila)} tarefa(s) sem responsavel serao CANCELADAS com o motivo '{motivo}':")
        for t in fila:
            print(f"   #{id_da_url(t['url'])}  {t['resumo'][:90]}")

        if args.simular:
            print("\nModo simulacao: abre cada tarefa e digita o motivo, mas clica em Voltar.")
        elif not args.sim:
            resp = input("\nConfirma CANCELAR essas tarefas no Tasky? [s/N] ").strip().lower()
            if resp not in {"s", "sim", "y"}:
                nav.close()
                print("Cancelado. Nada foi alterado.")
                return

        # o modal de cancelamento e HTML; um confirm() nativo aqui seria algo inesperado
        page.on("dialog", lambda d: d.dismiss())

        for n, tarefa in enumerate(fila, 1):
            tid = id_da_url(tarefa["url"])
            status, detalhe = "cancelada", f"motivo: {motivo}"
            try:
                page.goto(tarefa["url"], wait_until="domcontentloaded")
                page.wait_for_timeout(cfg.get("pausa_entre_tarefas_ms", 1200))
                conferir_sessao(page)
                if id_da_url(page.url) != tid:
                    raise RuntimeError(f"abriu a tarefa #{id_da_url(page.url)} em vez de #{tid}")
                erro = cancelar_tarefa(page, cfg, motivo, args.simular)
                if erro.startswith("SIMULACAO"):
                    status, detalhe = "simulado", erro
                elif erro:
                    raise RuntimeError(erro)
            except (PWTimeout, PWError, RuntimeError) as exc:
                status = "falha"
                detalhe = str(exc).splitlines()[0][:180]
                try:
                    page.screenshot(path=str(DIR_ERROS / f"{tid}.png"), full_page=True)
                except PWError:
                    pass

            resultados.append({
                "quando": datetime.now().isoformat(timespec="seconds"),
                "id": tid, "url": tarefa["url"], "tag": "",
                "status": status, "detalhe": detalhe,
            })
            print(f"[{n}/{len(fila)}] #{tid} -> {status} {detalhe}")

        nav.close()

    if args.simular:
        ok = sum(1 for r in resultados if r["status"] != "falha")
        print(f"\nSimulacao: {ok} de {len(resultados)} passariam. Nada foi gravado no Tasky.")
        return

    escrever_csv(ARQ_LOG, COLUNAS_LOG, resultados, acrescentar=True)
    ok = sum(1 for r in resultados if r["status"] == "cancelada")
    falhas = [r for r in resultados if r["status"] == "falha"]
    print(f"\n{ok} cancelada(s), {len(falhas)} com falha.")
    print(f"Log: {ARQ_LOG}")
    if falhas:
        print(f"Screenshots das {len(falhas)} falha(s) em: {DIR_ERROS}")


def cmd_verificar(cfg: dict, args):
    problemas = validar_projeto(cfg)
    erros = [p for p in problemas if p[0] == "ERRO"]
    for nivel, texto in problemas:
        print(f"{nivel}: {texto}")
    regras = carregar_json(ARQ_REGRAS).get("regras", [])
    print(f"\n{len(regras)} regra(s), {len(tags_oficiais())} tag(s) oficiais, "
          f"{len(erros)} erro(s) e {len(problemas) - len(erros)} aviso(s).")
    if erros:
        raise SystemExit(1)
    print("Configuracao consistente.")


def cmd_testar(cfg: dict, args):
    """Roda os casos de testes.json. Rede de seguranca ao mexer em regras.json."""
    if not ARQ_TESTES.exists():
        sair(f"{ARQ_TESTES} nao existe - sem casos de teste para rodar.")
    regras = carregar_json(ARQ_REGRAS)["regras"]
    oficiais = tags_oficiais()
    ruido = compilar_ruido(cfg)
    margem = int(cfg.get("margem_disputa", 8))
    casos = carregar_json(ARQ_TESTES)["casos"]

    falhas = 0
    for caso in casos:
        blocos = caso["mensagens"] if isinstance(caso.get("mensagens"), list) else [caso.get("texto", "")]
        v = classificar(blocos, regras, oficiais, ruido, caso.get("departamento", ""), margem)
        esperada = caso.get("tag_esperada", "")
        erros = []
        if normalizar(v.tag) != normalizar(esperada):
            erros.append(f"tag '{v.tag or '(nenhuma)'}' != esperado '{esperada or '(nenhuma)'}'")
        if caso.get("confianca_esperada") and v.confianca != caso["confianca_esperada"]:
            erros.append(f"confianca '{v.confianca}' != esperado '{caso['confianca_esperada']}'")
        if erros:
            falhas += 1
            print(f"FALHOU  {caso['nome']}\n        " + "\n        ".join(erros))
            if v.termo:
                print(f"        casou por '{v.termo}' na regra '{v.regra}'")
        elif args.detalhado:
            print(f"ok      {caso['nome']} -> {v.tag or '(nenhuma)'} ({v.confianca or '-'})")

    print(f"\n{len(casos) - falhas}/{len(casos)} caso(s) passaram.")
    if falhas:
        raise SystemExit(1)


def cmd_inspecionar(cfg: dict, args):
    """Mostra o que cada seletor do config encontra na pagina informada."""
    exigir_playwright()
    with sync_playwright() as pw:
        nav, ctx = abrir_contexto(pw, cfg, headless=args.headless)
        page = ctx.new_page()
        page.goto(args.url or cfg["url_tarefas"], wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        if not args.url:
            aplicar_filtro_tecnico(page, cfg)
        print(f"\nPagina: {page.url}\n")
        for chave in cfg["seletores"]:
            print(f"{chave}:")
            for s in sel(cfg, chave):
                s = s.replace("{coluna}", (cfg.get("colunas") or ["Pendente"])[0])
                try:
                    qtd = page.locator(s).count()
                except PWError as exc:
                    print(f"   [invalido] {s} -> {str(exc).splitlines()[0][:70]}")
                    continue
                amostra = ""
                if qtd:
                    amostra = " | " + (page.locator(s).first.inner_text() or "").strip().replace("\n", " ")[:60]
                print(f"  {'OK ' if qtd else '   '}{qtd:>3}x  {s}{amostra}")
            print()
        if not args.headless:
            input("ENTER para fechar o navegador... ")
        nav.close()


# --------------------------------------------------------------------------- cli

def main():
    p = argparse.ArgumentParser(
        description="Fecha tarefas no Tasky aplicando a tag conforme o historico do atendimento.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pg = sub.add_parser("login", help="loga no Tasky e salva a sessao")
    pg.add_argument("--manual", action="store_true",
                    help="abre o navegador para voce logar na mao (em vez de digitar e-mail e senha aqui)")

    pa = sub.add_parser("analisar", help="le as tarefas e gera relatorio.csv (nao altera nada)")
    pa.add_argument("--limite", type=int, default=0, help="processa no maximo N tarefas (0 = todas)")
    pa.add_argument("--coluna", action="append", default=[],
                    help="coluna do Kanban a varrer (repita para varias); sobrepoe o config.json")
    pa.add_argument("--headless", action="store_true", help="sem abrir a janela do navegador")

    pl = sub.add_parser("aplicar", help="aplica a tag e conclui as tarefas aprovadas no relatorio.csv")
    pl.add_argument("--limite", type=int, default=0, help="aplica no maximo N tarefas (0 = todas)")
    pl.add_argument("--sim", action="store_true", help="nao pedir confirmacao no terminal")
    pl.add_argument("--headless", action="store_true", help="sem abrir a janela do navegador")
    pl.add_argument("--devagar", type=int, default=None, metavar="MS",
                    help="camera lenta: pausa MS entre cada acao, para acompanhar na tela")
    pl.add_argument("--forcar", action="store_true",
                    help="conclui mesmo quando o Tasky avisa que ha mensagem nao lida no TendiChat")
    pl.add_argument("--simular", action="store_true",
                    help="ensaio geral: abre as tarefas e confere tudo, sem salvar nem concluir")

    pc = sub.add_parser("cancelar", help="cancela as tarefas 'Sem responsavel' com o motivo 'sem tarefa'")
    pc.add_argument("--motivo", default="", help="texto digitado no campo do cancelamento (padrao: config.json)")
    pc.add_argument("--limite", type=int, default=0, help="cancela no maximo N tarefas (0 = todas)")
    pc.add_argument("--coluna", action="append", default=[],
                    help="coluna do Kanban a varrer (repita para varias); padrao: pagina inteira")
    pc.add_argument("--sim", action="store_true", help="nao pedir confirmacao no terminal")
    pc.add_argument("--headless", action="store_true", help="sem abrir a janela do navegador")
    pc.add_argument("--devagar", type=int, default=None, metavar="MS",
                    help="camera lenta: pausa MS entre cada acao, para acompanhar na tela")
    pc.add_argument("--simular", action="store_true",
                    help="ensaio geral: digita o motivo e clica em Voltar, sem cancelar nada")

    sub.add_parser("verificar", help="valida config.json, regras.json e tags_tasky.json")

    pt = sub.add_parser("testar", help="roda os casos de testes.json contra as regras")
    pt.add_argument("--detalhado", action="store_true", help="mostra tambem os casos que passaram")

    pi = sub.add_parser("inspecionar", help="mostra quais seletores casam com a pagina")
    pi.add_argument("--url", default="", help="URL a inspecionar (padrao: a lista de tarefas)")
    pi.add_argument("--headless", action="store_true", help="sem abrir a janela do navegador")

    args = p.parse_args()
    cfg = carregar_json(ARQ_CONFIG)
    {"login": cmd_login, "analisar": cmd_analisar, "aplicar": cmd_aplicar,
     "cancelar": cmd_cancelar, "verificar": cmd_verificar, "testar": cmd_testar,
     "inspecionar": cmd_inspecionar}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
