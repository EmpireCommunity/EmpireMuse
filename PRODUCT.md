# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

(O produto tem duas superfícies: o painel web em `/painel` e a interface no Discord, feita de embeds, botões, selects e modais. O Impeccable cobre o painel web; a interface do Discord segue os limites de embed e componentes da plataforma.)

## Users

- Membros da comunidade EmpireHUB e de outros servidores que convidaram o bot: estão numa call de voz do Discord, no celular ou no desktop, e querem pedir e controlar música sem saber qual bot está livre.
- Admins de servidor (dono, Administrador ou Gerenciar Servidor): a equipe do dono E admins de outros servidores que convidaram o bot (confirmado pelo dono). Usam o painel web para configurar o servidor (player, DJ, aparência, canal de pedidos, playlists, geral) e o Discord para o dia a dia.
- Nenhum outro público foi nomeado.

## Product Purpose

Bot de música multi-instância do Discord, oficial da comunidade EmpireHUB: cinco bots em pool no mesmo processo tocam em várias calls do mesmo servidor ao mesmo tempo, com um canal de pedidos e um painel únicos por servidor e toda a configuração valendo para o servidor inteiro. O painel web (Discord OAuth2 + PKCE) gerencia servidores e preferências pessoais. Sucesso, nas palavras do dono: ser "o melhor bot de música já feito", pelas funcionalidades exclusivas que a equipe desenvolve e não vê em outros bots.

## Positioning

Funcionalidades exclusivas, não vistas em outros bots (confirmado pelo dono). As que já existem no código: pool de bots com despacho automático para a call de quem pediu; canal de pedidos único com painel que lista todos os players ativos; playlists do servidor iguais para todos os bots, com gêneros e ordem; 15 skins de player intercambiáveis; painel web orientado a hipertexto (Tornado + htmx + Pico CSS) sem build de JavaScript; integrações Last.fm e Rich Presence; auto-update verificado.

## Operating Context

- Discord: comandos de barra só no bot principal; reservas atendem por prefixo onde o principal não está; interface por embeds, botões, selects e modais, com limites de 25 opções por select, 5 botões por linha e 25 campos por embed. Texto sempre em pt-BR.
- Painel web: Tornado + htmx + Pico CSS, templates Tornado com autoescape, tema escuro, sessões em cookie `__Host-`, sem build de JavaScript. Páginas de configuração por servidor com formulários versionados (hash do conteúdo; conflito = recarregar).
- Hospedagem: Docker (imagem `empirecommunity/empiremuse`), Pterodactyl ou checkout git; produção na Hostinger do dono. Lavalink v4 como processamento de áudio.
- Processo de trabalho: contratos por item em `.local/specs/`, threat model do Vigil, gates por tier de risco; repositório público, por isso o mecanismo de defesa fica fora do guia.

## Capabilities and Constraints

- Python 3.14, disnake, aiohttp, tornado; nenhuma dependência nova sem decisão explícita. Lockfiles `requirements-*-lock.txt`.
- Painel web: TS/JS inexistente por decisão (htmx); CSS único `empiremuse/panel/static/css/nebulosa.css`; templates em `empiremuse/panel/templates/`.
- Discord: segurança de texto de terceiros (nomes, faixas, URLs) só via `md_link`/`md_code`/`escape_markdown`, mensagens com `AllowedMentions.none()`; call privada nunca expõe faixa nem quem pediu.
- Indecidido: métricas de sucesso (adoção, uso diário) não foram definidas pelo dono; registrar quando houver.

## Brand Commitments

- Nome: EmpireMuse. Comunidade: EmpireHUB (Discord `https://empirecommunity.org/discord`). Obra derivada do MuseHeart-MusicBot (zRitsu, GPL-2.0), com crédito mantido.
- Paleta "Nebulosa" (`docs/identidade-visual.md`): fundo `#0B0A1F`, azul `#3B82F6`, violeta `#8B5CF6` (cor principal e padrão dos embeds), magenta `#C026D3`, vermelho `#E11D48`.
- Assets oficiais em `assets/brand/` (logo-256.png, logo.svg, avatar.png, banner.png, avatares/, favicon.ico); ícones de plataformas em `assets/providers/`; emojis de aplicação em `assets/emojis/`.
- Preferência declarada pelo dono para o painel web (registrada sem expandir): "página bonita, responsiva, dinâmica; gosto de efeito glass, aero, efeitos de transição modernos; criatividade é bem-vinda; sem restrições" além da marca existente.
- Idioma: pt-BR em toda a interface.

## Evidence on Hand

- Capturas reais da interface e prévias das skins em `assets/docs/` (player, canal de pedidos, Last.fm, `skin_*_preview.png`).
- Código aberto em `github.com/EmpireCommunity/EmpireMuse`; CHANGELOG e docs em `docs/`.
- NÃO existem e NÃO podem ser inventados: relatos, depoimentos, avaliações, números de uso ou qualquer informação falsa (exigência explícita do dono).

## Product Principles

1. Pedir música leva segundos: quem está numa call não precisa saber qual bot está livre nem onde o painel está.
2. Uma configuração por servidor: o painel web e o Discord gravam o mesmo documento, e o que um mostra o outro vê.
3. Texto de terceiros nunca vira link, menção ou código executável; segurança antes de brilho.
4. Funcionalidades exclusivas com acabamento: cada recurso novo chega completo (estados vazios, erros com recuperação, celular e desktop).
5. Nada inventado: só evidência real, só o que o código faz.

## Accessibility & Inclusion

Alvo WCAG 2.2 AA no painel web (HTML semântico, foco visível, teclado, contraste na paleta Nebulosa, mobile-first de 360 px em diante), conforme o padrão da equipe; o dono não pediu nada além disso.
