# Registros de Decisão de Arquitetura (ADR)

Este diretório armazena os Registros de Decisão de Arquitetura (*Architecture Decision Records* — ADR) do projeto **EmpireMuse**.

As decisões capturam escolhas técnicas relevantes, motivações, alternativas descartadas e trade-offs assumidos pela equipe de desenvolvimento e agentes autônomos.

Para propor uma nova decisão técnica ou arquitetural, copie o modelo base [`0000-template.md`](0000-template.md) para o próximo identificador sequencial (ex.: `0002-novo-topico.md`), preencha as seções obrigatórias e adicione a nova entrada ao índice abaixo.

## Índice de Decisões

| ADR | Título | Status | Data |
| :--- | :--- | :--- | :--- |
| [0000](0000-template.md) | Modelo de Registro de Decisão Arquitetural | Proposto | 2026-10-14 |
| [0001](0001-manter-python.md) | Manter Python e Sanear em Vez de Reescrever em Node/TypeScript | Aceito | 2026-10-14 |
| [0002](0002-painel-web.md) | Painel Web Integrado com Login Discord, Templates Tornado e htmx | Aceito | 2026-10-15 |
| [0003](0003-jishaku-fork.md) | Fork EmpireCommunity/EmpireMuse-jishaku para o Disnake | Aceito | 2026-10-24 |
| [0004](0004-auto-update-proxy.md) | Auto-update Docker por proxy de cliente com modelo fixo | Aceito | 2026-10-27 |
| [0005](0005-logs-stdlib.md) | Log centralizado com a biblioteca padrão, redator único e agregador de repetidos | Aceito | 2026-10-27 |
| [0006](0006-painel-externo-e-generico.md) | Painel externo substitui o genérico somente no modo externo | Aceito | 2026-11-11 |
