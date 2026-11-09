# Guia de Contribuição — EmpireMuse

Agradecemos o seu interesse em contribuir para o **EmpireMuse**! Este documento define as diretrizes, padrões de código e fluxos de trabalho para colaboradores humanos e agentes autônomos.

---

## 1. Documentação Técnica e Configuração do Ambiente

Antes de submeter alterações de código, familiarize-se com a documentação do projeto e configure o ambiente local:

- 🏗️ **[Arquitetura do Sistema](../docs/architecture.md)**: Visão geral dos componentes, fluxo de áudio com Lavalink, roteamento do `BotPool` e persistência.
- 🛠️ **[Guia de Desenvolvimento e Setup Local](../docs/development.md)**: Requisitos (Python 3.14, `uv`, Java 17+), instalação e execução passo a passo.
- ⚠️ **[Problemas Conhecidos e Backlog Técnico](../docs/known-issues.md)**: Mapeamento de bugs catalogados, débitos e comportamentos sob saneamento.

---

## 2. Fluxo de Branches

Adotamos uma estratégia baseada no **GitHub Flow**, garantindo simplicidade e rastreabilidade:

1. A branch `main` é protegida e deve sempre refletir um estado estável e executável.
2. Todas as branches de trabalho devem ser criadas diretamente a partir da branch `main` atualizada.
3. Utilize o padrão de nomenclatura `tipo/descricao-curta`, em letras minúsculas e palavras separadas por hífen:
   - `feat/adicionar-filtro-audio`
   - `fix/recursao-token-spotify`
   - `refactor/modularizar-cogs-musica`
   - `docs/atualizar-guia-instalacao`
   - `test/adicionar-testes-player`
   - `perf/otimizar-consultas-mongo`
   - `chore/atualizar-dependencias`
   - `ci/configurar-github-actions`
4. Nunca envie commits diretamente para a branch `main`.

### Proteção da Branch Main e CODEOWNERS

- **Proteção da `main`**: A branch principal é protegida no GitHub. Commits diretos são bloqueados para todos os colaboradores (inclusive mantenedores). Não são permitidos *force-pushes* nem exclusão da branch.
- **Pull Requests obrigatórios**: Toda e qualquer alteração deve passar por um Pull Request. Todas as conversas e comentários de revisão devem estar resolvidos antes do merge.
- **Caminhos sensíveis (CODEOWNERS)**: Modificações em arquivos críticos de segurança, dependências, dados e atualização (mapeados em [`CODEOWNERS`](CODEOWNERS)) requerem obrigatoriamente a revisão e aprovação do mantenedor (`@Or4cu1o`).

---

## 3. Padrão de Mensagens de Commit (Conventional Commits)

Todas as alterações devem utilizar o padrão [Conventional Commits](https://www.conventionalcommits.org/pt-br/v1.0.0/). Isso garante um histórico limpo e geração automatizada de notas de lançamento.

### Formato da Mensagem

```
<tipo>(<escopo-opcional>): <descrição no imperativo e concisa>

[corpo opcional explicando o contexto, motivação e o "porquê"]

[rodapé opcional referenciando issues, ex.: Closes #123]
```

### Tipos Permitidos

| Tipo | Finalidade | Exemplo |
| :--- | :--- | :--- |
| `feat` | Introdução de nova funcionalidade | `feat(player): adicionar suporte a fila persistente` |
| `fix` | Correção de defeito ou bug | `fix(spotify): corrigir recursao em get_access_token` |
| `refactor` | Refatoração de código sem alteração funcional de comportamento | `refactor(cogs): extrair utilitarios de formatacao` |
| `docs` | Alterações exclusivas em documentação | `docs(contributing): incluir checklist de seguranca` |
| `test` | Criação ou ajuste de testes automatizados | `test(models): cobrir validacao de duracao de faixa` |
| `chore` | Manutenção rotineira, configuração de build ou dependências | `chore(deps): atualizar disnake para 2.12.2` |
| `perf` | Otimização de desempenho | `perf(database): indexar campo guild_id no banco` |
| `ci` | Ajustes em pipelines de integração contínua e automações | `ci(github): adicionar etapa de lint com ruff` |

---

## 4. Checklist para Pull Requests (PR)

Antes de solicitar a revisão de um Pull Request, certifique-se de preencher o modelo padrão em [`PULL_REQUEST_TEMPLATE.md`](PULL_REQUEST_TEMPLATE.md) e verificar os seguintes pontos obrigatórios:

- [ ] **Testes e Validação**: CI verde (lint, formatação, compilação de bytecode e suíte rodam no CI e não se repetem localmente) e testes manuais descritos no PR. Testes novos só para o que merece (ver [`.claude/rules/empiremuse/testing.md`](../.claude/rules/empiremuse/testing.md)).
- [ ] **Changelog Atualizado**: Registro resumido das alterações incluído na seção `[Unreleased]` de [`CHANGELOG.md`](../CHANGELOG.md).
- [ ] **Sem Segredos ou Credenciais**: Nenhuma chave de API, token de bot (`TOKEN`, `TOKEN_RESERVE_*`, `TOKEN_RADIO_*`), senha ou dado sensível foi incluído no histórico ou nos arquivos.
- [ ] **Revisão de Segurança Obrigatória**: Caso a alteração envolva entrada de dados de usuário, processamento de rede/RPC/WebSocket, leitura/escrita de arquivos ou operações no banco de dados, certifique-se de que foram seguidas as diretrizes de [`SECURITY.md`](SECURITY.md) (política e relato responsável) e o checklist técnico em [`.claude/rules/empiremuse/security.md`](../.claude/rules/empiremuse/security.md).
- [ ] **Padrão de Código**: Código aderente às regras de formatação, sem criação de blocos `except:` genéricos sem tratamento.
- [ ] **Commits Padronizados**: Mensagens de commit seguindo o formato Conventional Commits.

---

## 5. Licença e Direitos Autorais

O **EmpireMuse** é um software livre distribuído sob os termos da **GNU General Public License v2.0 (GPL-2.0)**.

- **Licenciamento de Contribuições**: Ao submeter qualquer código ou documentação para este repositório, você concorda que sua contribuição será licenciada sob a licença GPL-2.0.
- **Preservação de Créditos**: O EmpireMuse é derivado do projeto [MuseHeart-MusicBot](https://github.com/zRitsu/MuseHeart-MusicBot), criado por **zRitsu**. É mandatório preservar os avisos de direitos autorais, o arquivo `LICENSE` original e a devida atribuição ao projeto e ao autor original em todos os arquivos pertinentes.

---

## 6. Fluxo com Agentes Autônomos (Maestri)

O desenvolvimento deste repositório adota uma governança de inteligência artificial assistida por múltiplos agentes orquestrados na plataforma **Maestri**, operando sob as diretrizes do framework Everything Claude Code (ECC).

- As decisões estratégicas, divisão de escopo e aprovações são conduzidas pelo **MAESTRO**.
- Funções especializadas (como Especialista de Backend, Caçador de Vulnerabilidades/AppSec, Guardião de Git/Docs e Engenheiro de Tarefas Repetitivas) operam de forma isolada e cooperativa.
- Para entender as responsabilidades de cada papel, regras operacionais e comandos do ecossistema de agentes, consulte o documento:

👉 **[Governança de Agentes Maestri](../AGENTS.md)**
