# FechaTarefa — fechamento assistido de tarefas no Tasky

Automação para dar baixa nas tarefas de suporte no **Tasky** (`maxdatafortaleza.com.br`):
lê o histórico do atendimento, sugere a tag correspondente e conclui a tarefa.

## Para que foi criado

No dia a dia, cada atendimento encerrado no TendiChat vira uma tarefa na raia **Pendente**
esperando revisão. Fechar cada uma na mão é sempre a mesma sequência: abrir a tarefa, ler a
conversa, decidir a tag, clicar no lápis, adicionar a tag, salvar e concluir. Dezenas de vezes
por dia, com risco de esquecer a tag ou de fechar a tarefa errada.

O script faz essa sequência, mas **separa o que ele decide do que você decide**: primeiro ele só
lê e propõe (`analisar`), você confere num CSV, e só então ele escreve no Tasky (`aplicar`).
Não existe acesso ao código-fonte do Tasky — tudo é feito por automação de navegador
(Playwright + Chromium), exatamente como um usuário faria.

**Escopo padrão:** tarefas **de quem fez o `login`**, na coluna **Pendente**, cujo card contenha
`[TendiChat]`. Tudo isso é configurável em [`config.json`](config.json).

## Requisitos

- Python 3.12+
- Playwright com o Chromium instalado:
  ```bash
  pip install playwright
  python -m playwright install chromium
  ```

## Uso no dia a dia

```bash
python fechaTarefa.py login       # pede e-mail e senha do Tasky e guarda a sessão em sessao.json
python fechaTarefa.py analisar    # lê a SUA fila e escreve relatorio.csv (NÃO altera nada no Tasky)
                                  #    -> abra o relatorio.csv e revise
python fechaTarefa.py aplicar     # aplica a tag e conclui só as linhas marcadas
```

O `login` só é necessário quando a sessão expira (o Tasky a mantém por ~30 dias) ou para trocar
de usuário. O `aplicar` pede confirmação no terminal antes de escrever qualquer coisa.

### Cada analista com o próprio login

O script trabalha **na fila de quem fez o `login`**. Ele pergunta ao próprio Tasky quem está
logado (`/api/auth/session`): o `id` do usuário é o mesmo valor do filtro de técnicos
(Júlio Cesar = `3`, Elson Jr = `2`). Por isso nada no `config.json` precisa mudar de uma pessoa
para outra.

```text
> python fechaTarefa.py login
E-mail do Tasky: elson@...
Senha (nao aparece enquanto digita):
Login OK: Elson Jr (elson@...)
O 'analisar' vai trazer a fila de Elson Jr.
```

- A senha não aparece na tela e **não é gravada**. Só os cookies da sessão vão para `sessao.json`.
- Senha errada: o `login` pergunta de novo (até 3 vezes).
- O `login` sempre abre o navegador limpo, sem herdar a sessão de quem logou antes.
- O nome é conferido **inteiro**. Existem *Elson* (id 1) e *Elson Jr* (id 2), e "contém" confundiria
  os dois. Se o nome selecionado no filtro não bater com o da sessão, o script para.
- Para olhar a fila de **outra** pessoa, troque `"filtro_tecnico": "logado"` por um id fixo
  (ex.: `"2"`) e preencha `filtro_tecnico_nome` (ex.: `"Elson Jr"`).

### Instalando em outra máquina

1. Instale o Python 3.12+ (marque *Add python.exe to PATH*) e depois:
   ```bash
   pip install playwright
   python -m playwright install chromium
   ```
2. Copie a pasta do projeto **sem** `sessao.json`, `credenciais.json`, `relatorio.csv`,
   `log_aplicacao.csv`, `erros/` e `__pycache__/`. Os dois primeiros dão acesso à conta de quem
   copiou; os outros são o histórico dessa pessoa.
3. Rode `python fechaTarefa.py testar` (tem que dar tudo passando) e depois
   `python fechaTarefa.py login` com o seu e-mail e senha do Tasky.

Use o terminal do Windows, o PowerShell ou o do VS Code. No Git Bash (mintty) o Python não
enxerga o terminal e o `login` não consegue perguntar. Nele, rode `winpty python fechaTarefa.py login`.

### Revisando o `relatorio.csv`

Abre no Excel (`;` como separador). Uma linha por tarefa:

| coluna | o que é |
|---|---|
| `aprovado` | `x` = pode fechar. Apague o `x` para pular. Vira `feito` depois de concluída |
| `id`, `titulo`, `url` | identificação da tarefa |
| `departamento` | Suporte / Financeiro / Comercial — influencia a tag escolhida |
| `tag_sugerida` | a tag que será aplicada. **Pode ser editada à mão** |
| `confianca` | `alta` / `media` / `baixa` — veja abaixo. Só `alta` já vem com `x` |
| `regra` | qual regra decidiu (ou `nota do tecnico`) |
| `termo` | **a palavra exata que disparou a regra** — a conferência mais rápida do relatório |
| `trecho` | o pedaço do histórico em volta dessa palavra. **Quando nenhuma regra bate, traz uma amostra da conversa** — é com ela que você escreve a regra que falta |
| `alternativas` | outras tags que também bateram, com a palavra delas |
| `tags_atuais` | tags que a tarefa já tinha |
| `observacao` | por que ficou sem `x`, por que ficou sem tag, ou quando foi concluída |

Linhas sem tag ficam sem `x`: o robô não fecha o que não soube classificar.
Linhas com tag mas confiança abaixo de `alta` **também** ficam sem `x` — a sugestão está lá,
mas quem decide é você. Bater o olho na coluna `termo` resolve em segundos: se a tag diz
`CTE` e o termo diz `conectem`, é falso positivo.

## Comandos

| Comando | O que faz |
|---|---|
| `login` | Pergunta e-mail e senha do Tasky e salva `sessao.json`. Com `TASKY_EMAIL`/`TASKY_SENHA` definidas não pergunta. Sem terminal (execução automática) usa o `credenciais.json`, se existir. `--manual` abre o navegador para você logar na mão (útil se aparecer 2FA) |
| `analisar` | Percorre a fila, lê o histórico de cada tarefa e gera o `relatorio.csv`. **Somente leitura** |
| `aplicar` | Para cada linha aprovada: aplica a tag e conclui a tarefa. Grava `log_aplicacao.csv` |
| `verificar` | Valida `config.json`, `regras.json` e `tags_tasky.json` sem abrir navegador: tag inexistente, regex quebrada, nome de regra repetido, termo curto demais, regras empatadas com tags diferentes. Sai com erro se achar problema |
| `testar` | Roda os casos de [`testes.json`](testes.json) contra as regras. **Use sempre depois de mexer em `regras.json`** |
| `inspecionar` | Mostra quantos elementos cada seletor do `config.json` encontra na página. Use quando o Tasky mudar de layout e algo parar de funcionar |

`verificar` e `testar` também rodam sozinhos no `analisar`/`aplicar`: se a configuração
estiver inconsistente, o script para antes de tocar no Tasky.

Opções úteis:

| Flag | Onde | Para que |
|---|---|---|
| `--simular` | aplicar | **Ensaio geral**: abre cada tarefa aprovada, confere que a tag existe na lista e que os botões estão lá — e para antes de Salvar/Concluir. Nada é gravado |
| `--limite N` | analisar, aplicar | Processa no máximo N tarefas — ideal para testar |
| `--coluna NOME` | analisar | Outra raia do Kanban (`Em espera`, `Trabalhando`, `Pendente`, `Aguardando terceiros`). Repita a flag para várias |
| `--headless` | todos | Sem abrir a janela do navegador |
| `--devagar MS` | aplicar | Câmera lenta: pausa MS entre cada ação, para acompanhar na tela |
| `--forcar` | aplicar | Aceita **qualquer** `confirm()` do Tasky nessa rodada, não só os de `avisos_aceitos` |
| `--sim` | aplicar | Não pedir confirmação (para uso automatizado) |
| `--url URL` | inspecionar | Inspeciona outra página (ex.: uma tarefa específica) |

## Como a tag é escolhida

Na ordem — o primeiro que resolver, ganha:

1. **Nota do técnico.** Se alguma atualização curta da tarefa, escrita por você (sem o prefixo
   `[TendiChat]`), for exatamente o nome de uma tag oficial — `inatividade`, `Sem tarefa` —
   ela é usada e as regras nem rodam. É uma decisão manual escrita na própria tarefa.
2. **Regras de palavra-chave** ([`regras.json`](regras.json)), da maior para a menor `prioridade`.
3. **Departamento**, quando a regra tem `tag_por_departamento`: a mesma regra financeira rende
   `Financeiro Maxdata` no dep. Financeiro (cobrança nossa) e `Financeiro Sistema` no Suporte
   (módulo financeiro do sistema).

Antes de tudo isso o histórico passa por duas limpezas:

- **Mensagens automáticas são descartadas inteiras** (menu do bot, boas-vindas, "Ticket
  encerrado no TendiChat", anexos sem texto) — lista em `ignorar_blocos`. Sem esse filtro,
  o menu *"4- Chave de Ativação"* marcava tarefa errada.
- **Ruído é apagado de dentro do que sobrou** (`limpar_texto`): e-mails, links, telefone,
  protocolo `PRO-2026/…` e o marcador de autor `*julio cesar*`. Sem isso a assinatura
  `financeiro@maxdatafortaleza.com.br` sozinha marcava a tarefa como financeira.

### Confiança — por que nem toda sugestão vem marcada

Cada sugestão sai com uma nota, e só a partir de `aprovar_automatico` (padrão: `alta`)
a linha já vem com `x`:

| confiança | quando | vem com `x`? |
|---|---|---|
| `alta` | a regra ganhou sozinha, com termo específico | sim |
| `media` | regra guarda-chuva (`Duvida`, `Inatividade`, `SEM TAREFA`), ou um único termo com outro assunto rondando a conversa | não |
| `baixa` | bateu só por termo genérico (`palavras_fracas`), ou outra tag empatou na disputa | não |

Quem empatou aparece na coluna `alternativas`. Para afrouxar/apertar, mexa em
`aprovar_automatico` (`alta`/`media`/`baixa`/`nunca`) e `margem_disputa` no `config.json`.

### Anatomia de uma regra

```json
{
  "nome": "NFC-e",
  "tag": "Nfce",
  "prioridade": 84,
  "modo": "qualquer",
  "palavras": ["nfce", "nfc-e", "cupom fiscal eletronico"]
}
```

- **Termo simples casa palavra inteira.** `carne` **não** casa dentro de `Dicarnes`, `cte`
  não casa dentro de `conectem`, `nfe` não casa dentro de `infelizmente`. Era assim que
  entrava tag errada.
- **Termo terminado em `*` casa por prefixo**: `agend*` pega *agendar*, *agendamento*;
  `boleto*` pega *boletos*.
- Termos com prefixo `re:` são expressões regulares, usadas como estão.
- A comparação **ignora acentos e maiúsculas** — escreva as palavras sem acento.
- `modo: "qualquer"` — basta um termo aparecer em qualquer mensagem do histórico.
- `modo: "todas"` — todos os termos precisam estar **na mesma mensagem**. Sem essa restrição,
  "chave atualizada" numa mensagem e "boleto" vinte mensagens depois viravam `Boleto Atualizado`.
- `prioridade` — maior vence. Regras específicas em cima, genéricas embaixo
  (`SEM TAREFA` é 5, a última de todas).
- `palavras_fracas` (opcional) — termos genéricos demais para decidir sozinhos (`em aberto`,
  `pago*`, `contrato*`). Se **só** eles baterem, a linha sai com confiança `baixa` e sem `x`.
- `disputa_ok` (opcional) — nomes de regras cuja concorrência **você já decidiu de propósito**.
  `Boleto Atualizado` sempre ganha de `Financeiro (padrão)`, então essa disputa não rebaixa a
  confiança toda vez.
- `tag_por_departamento` (opcional) sobrepõe a tag conforme o departamento da tarefa.

As tags precisam existir em [`tags_tasky.json`](tags_tasky.json) (as 92 cadastradas no Tasky) —
`verificar` **impede a execução** se alguma regra apontar para tag inexistente. Se criarem tags
novas em `/admin/tags`, atualize esse arquivo.

### Mexeu nas regras? Rode os testes

[`testes.json`](testes.json) guarda casos reais — inclusive as armadilhas que já causaram tag
errada em produção:

```bash
python fechaTarefa.py testar            # 25/25 caso(s) passaram
python fechaTarefa.py testar --detalhado
```

**Quando aparecer uma tag errada numa tarefa de verdade, o primeiro passo é copiar o trecho
para `testes.json` com a tag certa** — aí sim corrigir a regra. Assim o mesmo erro não volta.

## Proteções

**Contra tag errada**

- **Palavra inteira, não pedaço de palavra** — a origem dos falsos positivos reais
  (`conectem` → `CTE`, `infelizmente` → `NF-e`).
- **Assinatura e ruído fora do jogo** antes de casar as regras (`limpar_texto`).
- **Termo não casa entre duas mensagens**: as mensagens são unidas por um separador.
- **Confiança**: só sugestão `alta` já vem aprovada; empate e termo genérico ficam para você.
- **A palavra que disparou a regra vai no relatório** (coluna `termo`) — conferir é olhar uma célula.
- **`verificar` barra a execução** se uma regra apontar para tag que não existe no Tasky.
- **`aplicar` recusa tag inexistente** digitada à mão no CSV, e corrige maiúsculas/acentos
  para o nome oficial antes de clicar.
- **O clique acontece só dentro do popover de tags**, com nome exato; se aparecer mais de uma
  opção idêntica, o script para em vez de chutar.
- **`testes.json`** trava os erros já conhecidos: eles não voltam sem o teste acusar.

**Contra escrita indevida**

- **Nada é escrito no Tasky pelo `analisar`.**
- **`--simular`** faz o percurso inteiro sem Salvar nem Concluir.
- **Filtra pela pessoa logada** e **confere o nome inteiro** que ficou no filtro, não só o
  `option value`. Se o Tasky renumerar os técnicos, ou se *Elson* aparecer onde devia estar
  *Elson Jr*, o script para em vez de fechar a fila de outra pessoa.
- **`aplicar` mostra com que conta vai escrever** (`Aplicando como: …`) antes de abrir as tarefas.
- **Confere que a tarefa aberta é a da linha do CSV** (id da URL) antes de editar.
- **Sessão expirada** derruba a execução com aviso, em vez de gerar relatório vazio.
- **Avisos do Tasky (`confirm()`)**: quando o Tasky pergunta *"Este atendimento tem N
  mensagem(ns) não lida(s). Deseja concluir mesmo assim?"*, o script **clica OK e conclui** —
  mas só nesse aviso. A lista do que pode ser aceito está em `avisos_aceitos` no
  `config.json`; qualquer `confirm()` fora da lista (um "deseja excluir?", por exemplo)
  continua sendo **recusado**, porque um diálogo desconhecido pode não ser um aviso inofensivo.
  Toda tarefa fechada por cima de um aviso sai marcada no `log_aplicacao.csv`
  (`aviso aceito: …`) e no resumo do terminal.
  Para voltar a recusar tudo: `"concluir_com_aviso": false`. Para aceitar **qualquer** diálogo
  numa rodada específica: `--forcar`.

**Depois de escrever**

- **A tag é conferida no que ficou gravado**: o script recarrega a tarefa depois do Salvar e
  confirma que o chip está lá. Antes ele confiava no que estava na tela do editor.
- **Tag já aplicada** é detectada e a edição é pulada — sem risco de duplicar ao reprocessar.
- **Status honesto no log**: `ok` (tag + concluída), `tag_aplicada` (a tag foi gravada mas a
  conclusão não passou — é o caso do aviso de mensagem não lida) e `falha` (nada foi feito).
  Antes os dois últimos apareciam juntos como `falha`, escondendo que a tag já estava no Tasky.
- **Confirmação pós-conclusão**: o script confere se o botão Concluir sumiu.
- **Falhas geram screenshot** em `erros/<id>.png`.
- Tarefas concluídas viram `feito` no relatório, então rodar de novo não as reprocessa.
- **CSV gravado por arquivo temporário** — uma queda no meio não corrompe o `relatorio.csv`.

## Arquivos

| Arquivo | Papel |
|---|---|
| `fechaTarefa.py` | O script |
| `config.json` | URLs, filtros (técnico, coluna, `[TendiChat]`), seletores da tela, limpeza do texto e nível de auto-aprovação |
| `regras.json` | Regras palavra-chave → tag |
| `testes.json` | Casos de regressão das regras (`python fechaTarefa.py testar`) |
| `tags_tasky.json` | As 92 tags oficiais do Tasky, com seus IDs |
| `credenciais.json` | **Opcional.** E-mail e senha, lidos só quando não há terminal para perguntar (execução automática) |
| `sessao.json` | Cookies da sessão de quem fez o último `login`. Define de quem é a fila |
| `relatorio.csv` | Saída do `analisar` / entrada do `aplicar` |
| `log_aplicacao.csv` | Histórico do que foi aplicado, com data, status e motivo das falhas |
| `erros/` | Screenshots das falhas |

**Segurança:** `credenciais.json` e `sessao.json` dão acesso à sua conta: não compartilhe, não
versione e não copie para outra máquina. O `credenciais.json` não é mais necessário para usar no
terminal, porque o `login` pergunta. Para automatizar sem ele, use variáveis de ambiente:

```bash
set TASKY_EMAIL=seu@email.com
set TASKY_SENHA=suasenha
```

## Quando algo parar de funcionar

O Tasky é uma aplicação Next.js e o script depende de seletores CSS da tela — uma reforma de
layout pode quebrá-los. O sintoma costuma ser "nenhum card encontrado" ou "botão X não encontrado".

```bash
python fechaTarefa.py inspecionar                                   # na lista de tarefas
python fechaTarefa.py inspecionar --url https://maxdatafortaleza.com.br/tarefas/2589
```

Ele lista cada seletor do `config.json` e quantos elementos casaram — os que aparecerem com `0`
são os que precisam ser corrigidos. Cada campo aceita **uma lista** de seletores, e o script usa o
primeiro que existir na página.

Se a sessão expirar (o `analisar` cair na tela de login), é só rodar `login` de novo.

**Tela de login crua (texto `:HL[...]` em vez do formulário).** O CDN do Tasky às vezes entrega
o `/login` guardado no formato interno do Next.js. O `login` já contorna isso com um parâmetro
único na URL e tenta 3 vezes. Se ainda falhar, use `login --manual`.

## Referência rápida da tela

Mapeado em 01/09/2026, para quem for dar manutenção:

- Lista: cards em `div.bg-white.rounded-xl.shadow-sm.p-3`, dentro da coluna
  `div.rounded-xl.border.p-3.space-y-2:not(.shadow-sm)`; link `a[href^='/tarefas/']`
- Filtro de técnico: `select:has(option:text-is('Todos os técnicos'))` (value = id do usuário:
  Júlio Cesar `3`, Elson Jr `2`, Elson `1`)
- Usuário logado: `GET /api/auth/session` → `{"user": {"id", "nome", "email", "papel"}}`
- Detalhe: histórico em `div.post-content`, departamento em `span.bg-indigo-50.text-indigo-600`
- Escrita: lápis `button[title='Editar tarefa']` → `+ Tag` → busca `input[placeholder='Buscar tag...']`
  → clique na tag → **clicar de novo em `+ Tag`** (o popover cobre o botão Salvar e o Esc não fecha)
  → `Salvar` → `Concluir`
