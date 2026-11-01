# ADR-0001: Manter Python e Sanear em Vez de Reescrever em Node/TypeScript

**Data**: 2026-10-14
**Status**: Aceito
**Decisores**: Maestro Orquestrador, Backend Specialist, AppSec & Vulnerability Hunter, Devil's Advocate, Git & Docs Keeper

## Contexto

O **EmpireMuse** é derivado do **MuseHeart-MusicBot** e herdou uma base de código consolidada com aproximadamente 34,5 mil linhas de código em Python 3.13, estruturada sobre o framework `disnake` (fixado historicamente no commit `96ed4459`, anterior à versão 2.12.0). A base de código atual apresenta passivos técnicos relevantes: ausência completa de testes automatizados, cerca de 365 blocos `except:` nus (não específicos) e defeitos pontuais catalogados em integrações (ex.: recursão em tokens do Spotify, falhas no RPC Tornado de `web_app.py`, riscos com serialização `pickle`).

Diante deste cenário, avaliou-se a viabilidade estratégica de realizar uma reescrita integral da aplicação em Node.js com TypeScript (utilizando `discord.js`), em contraposição a sanear, testar e modernizar a base existente em Python.

A avaliação foi conduzida sob o princípio mandatário estipulado pelo projeto: **manter integralmente todas as funcionalidades existentes** (tocador de música avançado, 15 skins dinâmicas de interface, arquitetura multi-bot via `BotPool`, servidor web/RPC e suporte dual a MongoDB e TinyMongo), sem realizar poda de escopo funcional.

## Decisão

**Decidimos manter o Python 3.13 como stack tecnológica do EmpireMuse e realizar um saneamento profundo da base existente**, em vez de executar uma reescrita completa em Node/TypeScript.

Como parte desta decisão:
1. **[Concluído]** `disnake` migrado do commit git `96ed4459` para a versão estável **2.12.2** do PyPI (com hashes no lock), junto com o fork `empiremuse-jishaku` (KI-30).
2. Planejar e executar a introdução de tipagem estática gradual, criação de suites de testes automatizados unitários/integração com `pytest`, eliminação de exceções genéricas e refatoração modular dos cogs monolíticos (como `modules/music.py` e `utils/music/models.py`).

### Fatos e Fundamentos Técnicos da Decisão

- **Custos e Prazos de Engenharia**: O esforço estimado para sanear, cobrir com testes e estabilizar a base Python existente é de **18 a 23 pessoa-semanas**. Já a reescrita integral em Node/TypeScript com paridade completa de recursos exigiria entre **43 e 54 pessoa-semanas** (mais que o dobro do esforço), com elevado risco de regressões em casos de borda e atraso substancial no lançamento.
- **Processamento de Áudio Desacoplado**: No EmpireMuse, 100% da decodificação pesada de áudio, aplicação de filtros, streaming e manutenção dos sockets de voz UDP ocorrem no servidor de áudio externo (**Lavalink v3/v4** ou **NodeLink**). O bot em Python gerencia apenas eventos de sinalização, comandos e mensagens no Discord. Portanto, reescrever o cliente em Node.js não traria ganhos perceptíveis de latência ou throughput de áudio.
- **Segurança Indiferente à Linguagem**: A análise de segurança demonstrou que os riscos identificados (serialização de dados persistidos, autenticação do RPC e integridade de downloads) decorrem do desenho arquitetural e de implementação, não da linguagem. Reescrever em TypeScript sob os mesmos padrões arquiteturais resultaria em riscos similares.
- **Suporte ao Protocolo de Criptografia de Voz DAVE**: A compatibilidade com o protocolo de criptografia fim-a-fim de voz do Discord (DAVE), exigido em Lavalink ≥ 4.2, depende estritamente do envio do campo `channelId` no payload de voice state. Essa adequação já foi realizada no cliente Wavelink interno no commit upstream `32f227e9` (`wavelink/player.py:318`).
- **Vitalidade da Biblioteca Disnake**: A biblioteca `disnake` permanece em manutenção ativa pela comunidade open-source, dispondo da versão estável recente `2.12.2` (08/10/2026), atendendo integralmente às demandas de componentes interativos, modais e comandos slash do Discord.

## Alternativas Consideradas

### Alternativa 1: Reescrita Completa em Node.js / TypeScript (discord.js)
- **Prós**: Ecossistema TypeScript robusto, verificação estática rigorosa em tempo de compilação, popularidade ampla da biblioteca `discord.js`.
- **Contras**: Esforço estimado excessivo (43–54 pessoa-semanas); necessidade de reimplementar do zero toda a lógica de filas, 15 skins de embeds, integrações com Lavalink e servidor RPC; alto risco de introduzir regressões sutis no comportamento esperado pelos usuários.
- **Por que não**: O custo em tempo e horas de engenharia não é justificado, pois não há benefício mensurável de desempenho em reprodução musical nem de segurança em relação ao saneamento da base Python.

### Alternativa 2: Poda de Escopo + Reescrita em TypeScript (Sugerida pelo Devil's Advocate)
- **Prós**: Reduziria o prazo da reescrita em TypeScript para um patamar próximo a 20 semanas, mantendo apenas um reprodutor de áudio enxuto com controles básicos.
- **Contras**: Descarte de recursos consolidados e diferenciais do projeto, como o painel web/RPC via Tornado, o catálogo de 15 skins visuais customizáveis, o suporte a múltiplos tokens por processo (`BotPool`) e as configurações granulares por servidor.
- **Por que não**: **Rejeitada categoricamente por violar o requisito primordial do projeto**, que exige a preservação integral de todas as funcionalidades legadas herdadas do bot original.

### Alternativa 3: Migração de Disnake para discord.py (Plano B)
- **Prós**: `discord.py` é a biblioteca mais estabelecida, duradoura e com maior ecossistema de suporte em Python.
- **Contras**: O código atual utiliza extensões e abstrações de UI e eventos específicas do `disnake`. Uma migração imediata demandaria retrabalho de sintaxe em dezenas de arquivos sem ganho técnico imediato sobre o `disnake 2.12.2`.
- **Por que não**: Mantida formalmente como **Plano B**. O `disnake 2.12.2` atende plenamente ao projeto no momento. A migração para `discord.py` só será disparada caso ocorram os gatilhos de reavaliação.

## Consequências

### Positivas
- **Velocidade de Entrega**: Economia de mais de 50% no tempo estimado de engenharia (18–23 semanas vs 43–54 semanas), permitindo focar em estabilização, testes e segurança imediatamente.
- **Preservação de Recursos**: Garantia de funcionamento contínuo de todas as 15 skins, cogs de música, integrações Last.fm e servidor RPC existente.
- **Modernização Gradual**: Adoção de tooling moderno em Python (Python 3.13, gerenciamento ultra-rápido com `uv`, testes com `pytest`, linters e formatadores modernos).

### Negativas
- **Débito Técnico Herdado**: Necessidade de sanear sistematicamente os ~365 blocos `except:` nus e modularizar arquivos de alta complexidade ciclompática (ex.: `music.py` com 7.876 linhas).
- **Dependência de Fork do Discord**: Dependência contínua da saúde do projeto open-source `disnake`.

### Riscos e Mitigações
- **Risco**: Quebra de compatibilidade em refatorações de código complexo sem testes.
  - **Mitigação**: Regra estrita de TDD — criar testes unitários e de regressão para cada cog ou função crítica antes de realizar alterações no código produtivo.
- **Risco**: Insegurança em serialização com `pickle` em filas e configurações.
  - **Mitigação**: Planejar no saneamento a substituição de `pickle` por formatos seguros e estruturados (JSON / BSON / MessagePack).
- **Risco**: Descontinuação da biblioteca `disnake`.
  - **Mitigação**: Encapsulamento das chamadas de API do Discord e adoção formal do Plano B (`discord.py`) ativado via gatilhos de reavaliação.

## Gatilhos de Reavaliação

Esta decisão arquitetural deverá ser formalmente revista se qualquer uma das seguintes condições for atendida:

1. A biblioteca `disnake` permanecer **12 meses consecutivos sem nenhum release estável ou commits ativos de manutenção** no repositório upstream.
2. A equipe de mantenedores passar a ser composta majoritariamente por desenvolvedores especializados em TypeScript/Node.js, sem capacidade ou interesse em manter código Python.
3. O Discord introduzir um recurso obrigatório e essencial ao funcionamento operacional do bot cuja implementação oficial ou tempestiva ocorra unicamente no ecossistema `discord.js`.

## Atualização (2026-10-27)

Runtime atualizado para Python 3.14 no item 6; a decisão de manter Python não muda.

> **Nota (2026-11):** Canal de pedidos → Jukebox; modo 24/7 → Radios.
