# Rádios 24/7

Guia do dono do bot e dos administradores de servidor para as rádios: o que são, quem libera, como ligar, o que o bot faz sozinho e como acompanhar o custo no host.

## O que é uma rádio

Uma rádio é um bot **reserva** tocando sem parar num canal de voz ou de palco. Ela fica na call mesmo vazia, pausa quando ninguém ouve e volta sozinha. Há dois tipos:

- **Personalizada:** toca em ciclo uma playlist do servidor (as mesmas do `/play` e do painel). Ela tem uma **posição virtual**: o relógio da rádio anda mesmo pausada, então ao voltar ela retoma onde estaria se tivesse tocado o tempo todo, inclusive depois de reiniciar o bot.
- **Ao vivo:** toca uma estação do diretório **Radio Browser**. Só estações desse diretório; **não há como informar uma URL própria**. O título que a estação anuncia aparece no painel, no status do canal e nas skins.

A rádio nunca usa o bot principal. Cada bot reserva toca no máximo uma rádio por servidor.

## Quem libera: premium

Só o **dono do bot** (quem está em `OWNER_IDS`) concede o premium de rádio a um servidor. A equipe da aplicação no Discord, o dono do servidor, Administrador e Gerenciar Servidor não bastam, e o painel web também não concede. Com `OWNER_IDS` vazio, ninguém concede.

Os comandos do dono são de prefixo e ocultos:

| Comando | O que faz |
| :--- | :--- |
| `radio-premium conceder <id do servidor>` | Libera o servidor para até 3 rádios. |
| `radio-premium revogar <id do servidor>` | Remove o premium e **desliga todas as rádios do servidor**, em todos os bots. |
| `radio-premium listar` | Lista os servidores com premium. |
| `radio-pausar-todas` | Para **todas** as rádios do processo (válvula de segurança). |
| `radio-retomar-todas` | Volta de onde parou, aos poucos (2 por vez, 1 s entre elas). |
| `radio-bot adicionar\|remover <TOKEN_2>`, `radio-bot listar` | Define quais bots reserva são **só de rádio**. O principal não pode. |

Detalhes:

- **Pausar todas** não desliga nada e não mexe nas vagas: as rádios ficam na call, o painel mostra "pausada pelo dono" e nem ouvinte novo, botão ou `/radio ligar` as religa. O estado sobrevive a reinício.
- **Bot só de rádio** não entra na escolha de bot livre do `/play`, não vira bot do painel da Jukebox e recusa comando de prefixo com um aviso. Um reserva comum também pode tocar rádio; o bot só de rádio é escolhido primeiro.

## Limite

**3 rádios por servidor**, contando as pausadas. **Não há limite global**: quantos servidores têm premium é decisão do dono, e é por isso que existem a válvula "pausar todas" e a seção [Custo por rádio](#custo-por-rádio).

## Ligar e desligar (administradores)

Com o servidor liberado:

- `/radio ligar canal: [playlist:] [estacao:]`: liga uma rádio no canal de voz ou palco. Escolha a playlist **ou** a estação.
- `/radio desligar canal:`: desliga.
- `/radio status`: mostra as rádios do servidor.

Quem liga precisa de **Gerenciar Servidor** e de **Gerenciar Mensagens** no canal da rádio (conferidos na hora). Os comandos têm cooldown de 10 s por servidor e só funcionam em servidor.

Na call a rádio mantém um **painel** com os botões pausar, próxima e desligar (exigem Gerenciar Servidor). Se alguém apagar a mensagem do painel, o bot a recria.

### Rádio ao vivo

- **Busca:** a opção `estacao` completa pelo Radio Browser (2 a 64 caracteres). Sem premium ou sem Gerenciar Servidor a lista vem vazia.
- **Estação reserva:** na página **Rádios** do painel web dá para escolher uma estação reserva por rádio.
- **Queda:** se a estação cai, o bot tenta de novo após 2, 4, 8, 16 e 32 s (com variação aleatória). Esgotado, passa para a reserva, se estiver boa, e o painel mostra "reserva no ar". A principal volta quando o teste de saúde a liberar.
- **Indisponível:** uma estação que esgota 3 ciclos em 1 h é marcada como indisponível por 30 min. Ela aparece com ⛔ na busca e não pode ser escolhida.
- **Teste de saúde:** a cada 10 min o bot reexamina só as estações indisponíveis que alguma rádio **ligada** usa (até 20 por ciclo), sem contar clique no diretório.
- **Estação e reserva fora:** a rádio fica na call em silêncio e o painel mostra "fora do ar".

## O que o bot faz sozinho

- **Pausa por presença:** sem ouvinte (alguém na call que não esteja ensurdecido) por 30 s, a rádio para a faixa e guarda a posição. Cinco segundos depois de chegar um ouvinte, ela volta.
- **Reconexão:** qualquer queda, troca de canal ou desconexão (**inclusive feita por um membro**) leva a nova tentativa com espera crescente (5, 10, 20, 40 e 60 s, depois a cada 60 s). Canal apagado, permissão de conectar perdida ou premium revogado desligam a rádio.
- **Fila de retomadas:** na subida do bot, num reinício do Lavalink ou na volta do gateway, as rádios voltam 2 por vez, com 1 s entre elas, para não gerar pico.
- **Palco:** o bot tenta falar sozinho (precisa de Silenciar Membros); sem a permissão, pede a palavra e avisa; uma recusa do Discord vale uma tentativa por conexão.

## Regras do chat da call

O bot apaga mensagens **só no canal da rádio**, nunca no da Jukebox, e nunca apaga a mensagem fixada, o painel nem os avisos do bot. Elas valem para o público: **mensagens de quem ouve somem**. Os valores se configuram por rádio, com Gerenciar Servidor e Gerenciar Mensagens, na página **Rádios** do painel:

| Canal | Regra | Padrão | Faixa |
| :--- | :--- | :--- | :--- |
| Voz | apaga a mensagem depois de X segundos | 30 s | 5 a 600 s |
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

## Riscos aceitos e o que não está incluído

- Mensagens do público no chat da rádio **são apagadas** (decisão do dono).
- Não há limite global de rádios: o controle é o premium e a pausa geral.
- Fora desta versão: URL informada pelo usuário, estações HLS e rádio no bot principal.
