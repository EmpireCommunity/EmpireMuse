# Rádios

Guia do dono do bot e dos administradores de servidor para as **Radios**: o que são, quem libera, como configurar e ligar, como criar estações, o que o bot faz sozinho, os créditos das fontes e como acompanhar o custo no host.

O bot tem duas funções de canal: a **Jukebox** (canal de texto com painel e pedidos) e as **Radios**, que este guia descreve. Os comandos aparecem em inglês e, no Discord em pt-BR, com o nome traduzido entre parênteses.

## O que é uma rádio

Uma rádio é um bot **reserva** tocando sem parar num canal de voz ou de palco. Ela fica na call mesmo vazia, pausa quando ninguém ouve e volta sozinha. Há dois tipos (modos):

- **`custom` (personalizada):** simula uma rádio 24 h a partir de uma **estação** criada no servidor. A ordem é aleatória e a rádio evita repetir faixa e artista por perto. Ela **não tem posição virtual**: ao voltar de uma pausa ou de um reinício, toca uma faixa nova. Veja [Estações](#estações-personalizadas).
- **`live` (ao vivo):** toca uma estação do diretório **Radio Browser**, escolhida por local ou por nome. Só estações desse diretório; **não há como informar uma URL própria**. O título que a estação anuncia aparece no painel, no status do canal e nas skins. Ela tem uma **posição virtual**: o relógio da rádio anda mesmo pausada.

A rádio nunca usa o bot principal. Cada bot reserva toca no máximo uma rádio por servidor.

## Quem libera: premium

Só o **dono do bot** (quem está em `OWNER_IDS`) concede o premium de rádio a um servidor. A equipe da aplicação no Discord, o dono do servidor, Administrador e Gerenciar Servidor não bastam, e o painel web também não concede. Com `OWNER_IDS` vazio, ninguém concede.

Os comandos do dono são de prefixo e ocultos:

| Comando | O que faz |
| :--- | :--- |
| `radio-premium conceder <id do servidor>` | Libera o servidor para até 3 rádios. |
| `radio-premium revogar <id do servidor>` | Remove o premium e **desliga todas as rádios do servidor**, em todos os bots. |
| `radio-premium listar` | Lista os servidores com premium. |
| `radio-pause-all` (alias `radio-pausar-todas`) | Para **todas** as rádios do processo (válvula de segurança). |
| `radio-resume-all` (alias `radio-retomar-todas`) | Volta de onde parou, aos poucos (2 por vez, 1 s entre elas). |

Detalhes:

- **Pausar todas** não desliga nada e não mexe nas vagas: as rádios ficam na call, o painel mostra "pausada pelo dono" e nem ouvinte novo, botão ou `/radio on` as religa. O estado sobrevive a reinício.
- **Bot só de rádio** não entra na escolha de bot livre do `/play`, não vira bot do painel da Jukebox e recusa comando de prefixo com um aviso. Um reserva comum também pode tocar rádio; o bot só de rádio é escolhido primeiro.

## Limite

**3 rádios por servidor**, contando as pausadas. **Não há limite global**: quantos servidores têm premium é decisão do dono, e é por isso que existem a válvula "pausar todas" e a seção [Custo por rádio](#custo-por-rádio).

## Configurar e ligar (administradores)

Com o servidor liberado:

- `/radio setup` (`/radio configurar`): escolhe o canal de voz ou palco, o bot reserva (a lista traz só os reservas livres) e o tipo, `live` ou `custom`.
  - `live` segue para o seletor de local (veja [Rádio ao vivo](#rádio-ao-vivo)).
  - `custom` segue para o seletor de estações, com a dica para criá-las em `/radio station create`. Sem a chave do Last.fm no bot (`LASTFM_KEY`), o seletor avisa que as estações de gênero e de artistas estão indisponíveis.
  - Trocar o bot grava a configuração nova primeiro e só então desliga a rádio antiga.
- `/radio on` (`/radio ligar`): liga a rádio configurada. Sem estação escolhida, o comando recusa e indica o `/radio setup`.
- `/radio off` (`/radio desligar`): desliga.
- `/radio status`: mostra as rádios do servidor.
- O comando de prefixo `radio` (aliases `rd` e `247`) mostra o status, com botões para as ações.

Quem configura e liga precisa de **Gerenciar Servidor** e de **Gerenciar Mensagens** no canal da rádio (conferidos na hora). Os comandos têm cooldown de 10 s por servidor e só funcionam em servidor.

Na call a rádio mantém um **painel** com os botões pausar, próxima e desligar. Desligar exige Gerenciar Servidor. Os demais valem para quem está na call do bot. Se alguém apagar a mensagem do painel, o bot a recria.

Bots só-rádio são definidos por `TOKEN_RADIO_<NOME>` no ambiente, sem configuração no banco. A escolha de bot na rádio 24 h lista os só-rádio antes das reservas; o principal nunca pode ser escolhido.

### Rádio ao vivo

- **Escolha por local:** Região → País → Estado (ou grupo de estados) → Cidade, no Discord e na página **Rádios** do painel. O atalho **⭐ {país}**, sempre o primeiro item, leva direto ao país do idioma do servidor, quando ele está na lista. Escolher uma cidade devolve as rádios dela e do entorno (100 km). A lista de locais é fixa e vem junto do bot: cobre 91 países, e quem ficou de fora continua alcançável pela busca por nome.
- **Busca por nome:** a opção `station` completa pelo Radio Browser (2 a 64 caracteres). Sem premium ou sem Gerenciar Servidor a lista vem vazia.
- **Estação reserva:** na página **Rádios** do painel web dá para escolher uma estação reserva por rádio.
- **Queda:** se a estação cai, o bot tenta de novo após 2, 4, 8, 16 e 32 s (com variação aleatória). Esgotado, passa para a reserva, se estiver boa, e o painel mostra "reserva no ar". A principal volta quando o teste de saúde a liberar.
- **Indisponível:** uma estação que esgota 3 ciclos em 1 h é marcada como indisponível por 30 min. Ela aparece com ⛔ na busca e não pode ser escolhida.
- **Teste de saúde:** a cada 10 min o bot reexamina só as estações indisponíveis que alguma rádio **ligada** usa (até 20 por ciclo), sem contar clique no diretório.
- **Estação e reserva fora:** a rádio fica na call em silêncio e o painel mostra "fora do ar".

## Rádio ao vivo pela Jukebox (modo rádio)

Em servidor com o acesso Limitado a Apoiadores, o select da Jukebox ganha no topo da primeira página a entrada **📻 Rádios ao Vivo**. Qualquer membro numa call pode usá-la, sem Gerenciar Servidor e sem canal de rádio: o seletor abre só para quem clicou, com 🔎 **Buscar por nome**, ⭐ **Minhas favoritas**, ⭐ **Favoritas do servidor**, 📌 **Meus filtros** e os locais. Escolhida a estação, **Tocar na minha call** liga o modo rádio no bot de música que a Jukebox escolhe (o que já está na sua call, senão o primeiro livre; nunca um bot só de rádio nem um que toca rádio 24 h).

- **Quem decide:** com o player ocioso e a fila vazia, toca na hora. Com música tocando, fila ou outra rádio, DJ e quem gerencia o servidor tocam direto; os demais abrem uma votação no canal do player (60 s, metade de quem está na call com o áudio ligado, até 3 votos; sozinho, passa no ato). No modo restrito, só DJ. Ao passar, a fila é limpa e a música atual para.
- **Pedir música no modo rádio** (`/play`, comando de prefixo, mensagem no canal da Jukebox, favoritas, "Adicionar na fila", ⏮ e `/readd`): antes de qualquer efeito, quem pediu recebe um aviso com dois botões, por 45 s, e só essa pessoa pode responder. O DJ (ou quem está sozinho na call) vê **Parar a rádio e tocar**; o membro vê **Abrir votação**, e se ela passar a rádio para e ele pede a música de novo. **Cancelar** deixa a rádio tocando. No modo restrito, com uma votação já aberta ou logo depois de uma votação sua expirar, a recusa vem direto, sem aviso. Na barra e nos botões o aviso é efêmero (só você vê); no prefixo e no canal da Jukebox ele responde à sua mensagem, então quem está no canal pode ler, mas só você pode clicar.
- **Não toca:** em call que já tem uma rádio 24 h, sem o acesso do servidor, ou num bot sem o Lavalink local.
- **No modo rádio:** não há fila nem repetir; ⏭ para a rádio e deixa o player ocioso; retomar depois de pausar toca o ao vivo de agora; a estrela do player guarda a estação nas suas favoritas. Trocar de rádio de novo espera 5 s. O `/change_node` é recusado (a rádio ao vivo só toca no Lavalink local); se o player perder o nó local, a rádio tenta voltar a ele como numa queda. Para ligar a rádio num bot que já está numa call, entre nessa call.
- **Queda:** o bot tenta de novo após 2, 4 e 8 s. Sem volta, avisa no canal do player e fica ocioso.
- **Suas favoritas:** o `/fav_manager`, modo **Rádios favoritas**, ganha **Tocar** (na sua call, com as mesmas regras).

## Estações na Jukebox (Rádios do Servidor)

Com o acesso Limitado a Apoiadores, a primeira página do select da Jukebox também traz **🎙️ Rádios do Servidor**. O seletor abre só para quem clicou, com as estações do servidor (as suas ⭐ primeiro) e os gêneros prontos, internacionais e brasileiros.

- **Como o autoplay:** a estação começa depois da música atual (ou na hora, com o player parado ou no modo rádio ao vivo). Uma música pedida toca antes, e a estação volta depois dela.
- **Quem decide:** com a fila vazia ou só com as suas músicas, toca direto; com músicas de outras pessoas na fila, DJ e quem gerencia o servidor tocam direto e os demais abrem uma votação. A fila é limpa quando a estação começa. No modo restrito, com músicas de outras pessoas na fila ou com uma rádio ou estação já ligada, só DJ.
- **Desligar:** o botão de autoplay do player mostra "📻 Estação: {nome}"; desligá-lo encerra a estação.
- **Sem faixas:** se 5 faixas seguidas não tocarem, a estação para e avisa no canal do player. "Repetir fila" repete só as músicas pedidas.
- **Estrela:** com uma faixa da estação tocando, a estrela do player guarda a estação nas suas favoritas deste servidor.
- **Não toca:** em call que já tem uma rádio 24 h ou sem o acesso do servidor.

## Estações personalizadas

Os **33 gêneros prontos** são virtuais, disponíveis com premium em **Gêneros internacionais** (21) e **Gêneros brasileiros** (12) no seletor da rádio 24 h. A página de estações do painel permite consultá-los; eles não podem ser criados, editados ou apagados. Anterior/Próxima e a escolha aleatória continuam somente nas estações do servidor; os gêneros são escolhidos pelo seletor.

Uma estação do servidor tem um **nome** (1 a 32 caracteres, único no servidor) e **uma** fonte:

| Fonte | O que é | Limite |
| :--- | :--- | :--- |
| Artistas | uma lista de nomes de artistas | 1 a 10 nomes de até 64 caracteres |
| Playlists | playlists do servidor (as mesmas do `/play` e do painel) | 1 a 5 playlists |

- **Gerenciar:** `/radio station create`, `edit`, `delete` e `list` (`/radio estação criar|editar|apagar|listar`), ou a seção **Estações** da página Rádios do painel. Os dois usam as mesmas regras. Exigem **Gerenciar Servidor**.
- **Limites:** até 10 estações por servidor e até 300 faixas por estação. Apagar uma estação em uso é recusado, e a mensagem diz qual call a usa.
- **Como toca:** a ordem é aleatória. Uma faixa não volta tão cedo, e o mesmo artista não se repete em sequência. As próximas faixas são resolvidas enquanto a atual toca. Se 5 faixas seguidas falham, a rádio pausa, o painel mostra "estação sem faixas tocáveis" e ela tenta de novo em 10 min.
- **Playlists que sumiram:** se a playlist de uma estação for apagada, a estação mostra "sem faixas".
- **Artista digitado:** se o Last.fm não reconhece o nome, ele aparece com ⚠️ nas duas interfaces. O salvamento não é bloqueado.
- **Troca de estação na call:** os botões ⏮ e ⏭ do painel trocam para a estação anterior ou seguinte do servidor (por ordem de nome), e há um botão para pular a faixa. Valem só para quem está na call do bot.
- **Migração dos gêneros antigos:** na subida, as referências das rádios e das favoritas passam ao gênero pronto de mesmo id (`g:<id>`). A estação antiga é apagada depois, sem backup. Conflitos de gravação preservam a estação para a próxima subida; repetir a migração é seguro. Gênero fora do catálogo é removido e a rádio conserva a referência antiga, mostrando estação sem faixas. Documentos sem identidade válida ficam inertes.
- **Favoritas do servidor (J3):** a lista do servidor (até 24 estações) só é alterada por quem tem Gerenciar Servidor, administrador ou é dono: no Discord por `/radio favoritas` (efêmero, Remover e Limpar com confirmação) e no painel, em Rádios › Favoritas do servidor (Remover com confirmação e Adicionar pelo seletor de local). Cada membro tem também a sua lista pessoal e os locais salvos: aparecem só para ele, em `/fav_manager` › Rádios favoritas (Remover e Limpar) e em Minha conta › Rádios favoritas (Remover). Nenhuma delas é migrada da outra.
- **Filtro antigo de gênero:** uma rádio que ainda guarda `f:genre` passa a mostrar todas as estações do servidor; escolha um dos dois grupos de gêneros prontos no seletor.
- **Quem configurou antes:** uma rádio personalizada que tocava uma playlist passa a usar uma estação de playlists com o mesmo nome, criada sozinha na subida do bot.

## O que o bot faz sozinho

- **Pausa por presença:** sem ouvinte (alguém na call que não esteja ensurdecido) por 30 s, a rádio para a faixa e guarda a posição. Cinco segundos depois de chegar um ouvinte, ela volta.
- **Reconexão:** qualquer queda, troca de canal ou desconexão (**inclusive feita por um membro**) leva a nova tentativa com espera crescente (5, 10, 20, 40 e 60 s, depois a cada 60 s). Canal apagado, permissão de conectar perdida ou premium revogado desligam a rádio.
- **Fila de retomadas:** na subida do bot, num reinício do Lavalink ou na volta do gateway, as rádios voltam 2 por vez, com 1 s entre elas, para não gerar pico.
- **Palco:** o bot tenta falar sozinho (precisa de Silenciar Membros); sem a permissão, pede a palavra e avisa; uma recusa do Discord vale uma tentativa por conexão.

## Regras do chat da call

O bot apaga mensagens **só no canal da rádio**, nunca no da Jukebox, e nunca apaga a mensagem fixada, o painel nem os avisos do bot. Elas valem para o público: **mensagens de quem ouve somem**. Os valores se configuram por rádio, com Gerenciar Servidor e Gerenciar Mensagens, na página **Rádios** do painel. Nessa página o tipo, o canal e o bot da rádio aparecem só para leitura: para mudá-los use `/radio setup`.

| Canal | Regra | Padrão | Faixa |
| :--- | :--- | :--- | :--- |
| Voz | apaga a mensagem depois de X segundos | 60 s | 5 a 600 s |
| Palco | apaga as mais antigas quando passam de N mensagens | 7 | 3 a 50 |
| Palco | apaga as com mais de X segundos | 600 s (10 min) | 60 a 3600 s |

A limpeza é em lote (poucas chamadas à API do Discord, mensagens com menos de 14 dias). Se o bot perder **Gerenciar Mensagens** ou **Ver Histórico de Mensagens**, a regra se desliga e o painel avisa.

## Acompanhar a carga: o resumo periódico

O resumo do `logs_loop` (ver [Logs e Telemetria Local](logs.md)) traz, no fim da primeira linha, o total do processo:

```
… · rádios: 5 ligadas (3 tocando, 2 pausadas)
```

- **ligadas:** rádios com a chave ligada (contam para o limite); inclui as pausadas pela presença ou pelo dono.
- **tocando:** a faixa está saindo agora (na rádio ao vivo, com o relay aberto).
- **pausadas:** ligadas e paradas (sem ouvinte ou pausadas pelo dono).

O número nunca traz servidor, canal ou estação. Cada ligar ou desligar também gera um INFO `radio.toggle` com o total.

Em cada linha de nó do Lavalink aparecem os quadros do último minuto:

```
nó <nome>: players N · CPU x% · memória y MiB · quadros enviados A anulados B déficit C
```

- **enviados:** quadros de áudio entregues ao Discord.
- **anulados:** quadros que o Lavalink não conseguiu tocar e substituiu por silêncio.
- **déficit:** quadros que faltaram para manter o ritmo. Em condições boas, anulados e déficit ficam perto de zero, e déficit crescendo com mais rádios ligadas indica que o host está no limite.
- **—** significa que o Lavalink não enviou o dado (por exemplo, sem players).

## Custo por rádio

> TODO (Litmus): preencher com a medição ao vivo de 3 rádios tocando: RSS do Java e do Python, CPU e `frameStats`, e o custo estimado por rádio. Com isso o dono decide quantos servidores liberar.

## Créditos das fontes

- **Last.fm:** os gêneros e os artistas das estações vêm do Last.fm. O painel da rádio e a página Rádios mostram **"Powered by Last.fm"**, com link. O bot guarda só o que mostra e toca, e limita as chamadas a 1 por segundo (rajada de 3), separado do scrobble.
- **GeoNames:** os dados de localização (cidades e estados) vêm do GeoNames, licença **CC BY 4.0**. O rodapé da página Rádios mostra "Dados de localização: GeoNames, CC BY 4.0", e a atribuição completa fica no `CREDITS.md`.
- **Radio Browser:** o diretório das estações ao vivo é de domínio público. O bot se identifica com um User-Agent próprio.

## Atualizar de uma versão anterior

Quem atualiza de uma versão anterior à release de Rádios e Jukebox deve configurar novamente o canal de pedidos e o painel antigos da Jukebox. A migração de subida `radios_release` foi removida e não guarda mais cópia. O reset de uma rádio também apaga sua configuração sem cópia, mesmo quando a tela de confirmação está com uma revisão antiga.

## Voltar para uma versão anterior

Depois que a migração R5 rodar, voltar para uma versão **anterior à release RADIOS** perde as rádios personalizadas migradas: o bot antigo não lê essas estações e as rádios delas não sobem. Não há cópia automática em `migration_backup` para restaurar.

## Riscos aceitos e o que não está incluído

- Mensagens do público no chat da rádio **são apagadas** (decisão do dono).
- Não há limite global de rádios: o controle é o premium e a pausa geral.
- **Uso comercial do Last.fm:** hoje o premium é concedido sem cobrança. Se um dia ele for vendido, é preciso fechar acordo com o Last.fm (partners@last.fm) **antes**, porque os termos de uso da API não cobrem uso comercial.
- Fora desta versão: URL informada pelo usuário, estações HLS, rádio no bot principal, Spotify e YouTube Music como fonte de gênero ou de artistas, e busca ou fila do usuário na rádio.
