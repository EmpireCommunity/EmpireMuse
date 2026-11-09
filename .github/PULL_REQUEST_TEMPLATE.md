## Resumo

Breve descrição das alterações propostas, incluindo o contexto, problema resolvido e motivação técnica.

<!-- Exemplo: Este PR corrige o vazamento de conexões no player de áudio e atualiza a serialização do payload DAVE. -->

---

## Tipo de Mudança

Marque com um `x` as opções aplicáveis:

- [ ] `feat` (Nova funcionalidade no bot ou interface)
- [ ] `fix` (Correção de defeito/bug)
- [ ] `refactor` (Refatoração de código sem alteração no comportamento externo)
- [ ] `docs` (Alterações ou adições na documentação)
- [ ] `test` (Adição ou correção de testes automatizados)
- [ ] `chore` (Manutenção interna, dependências, scripts ou build)
- [ ] `perf` (Melhoria mensurável de desempenho)
- [ ] `ci` (Alterações em fluxos de integração contínua ou automações)

---

## Como Testou

Descreva detalhadamente os procedimentos executados para validar as alterações:

1. **Validação de código e testes**:
   <!-- CI verde (lint, suíte e compilação rodam no CI) + teste manual descrito no PR -->
2. **Testes manuais**:
   <!-- Cenários simulados (ex.: reprodução de faixa Spotify/YouTube via Lavalink, comando /play, desconexão de canal) -->
3. **Ambiente utilizado**:
   <!-- SO, versão do Python (3.14), versão do Lavalink/NodeLink -->

---

## Checklist de Qualidade

Antes de solicitar a revisão, confirme se todos os critérios foram atendidos:

- [ ] Meu código segue as diretrizes de formatação e boas práticas do projeto.
- [ ] Realizei uma auto-revisão detalhada do código submetido.
- [ ] Validação realizada: CI verde (lint, suíte e compilação rodam no CI e não se repetem localmente) + teste manual descrito no PR.
- [ ] O arquivo [`CHANGELOG.md`](https://github.com/EmpireCommunity/EmpireMuse/blob/main/CHANGELOG.md) foi atualizado na seção `[Unreleased]`.
- [ ] **Segurança**: Confirmo que nenhum segredo, chave de API, credencial ou token (`TOKEN`, `TOKEN_RESERVE_*`, `TOKEN_RADIO_*`, etc.) foi incluído no repositório.
- [ ] **Auditoria de Segurança**: Se o PR manipular entrada de dados de usuário, rede/WebSocket, chamadas externas ou banco de dados, foi realizada revisão preventiva contra vulnerabilidades (conforme [`SECURITY.md`](https://github.com/EmpireCommunity/EmpireMuse/blob/main/.github/SECURITY.md) e [`.claude/rules/empiremuse/security.md`](https://github.com/EmpireCommunity/EmpireMuse/blob/main/.claude/rules/empiremuse/security.md)).
- [ ] Os títulos dos commits seguem o padrão [Conventional Commits](https://www.conventionalcommits.org/pt-br/v1.0.0/).
- [ ] A licença **GPL-2.0** e os créditos de autoria ao projeto original **MuseHeart-MusicBot** e **zRitsu** foram integralmente preservados.

---

## Issues Relacionadas

Closes #
