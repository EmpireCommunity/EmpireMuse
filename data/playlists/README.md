# Playlists importáveis

Esta pasta guarda listas de playlists que os administradores de um servidor podem importar pelo comando
`/playlists importar arquivo:<nome>`. Cada arquivo `.json` daqui (nome em minúsculas, números e hífen) é uma lista importável;
hoje há `playlists-empire.json`, a lista oficial da comunidade (somente URLs públicas).

As playlists do servidor são uma lista só, a mesma para todos os bots. Quem pode importar e exportar: dono do servidor,
Administrador ou quem tem Gerenciar Servidor. Também dá para importar um anexo `.json` (até 64 KiB) ou colar o texto, e
`/playlists exportar` gera um arquivo no mesmo formato.

## Formato

Um objeto com a chave `playlists`, que é uma lista de playlists. Chaves em inglês; `order` define a posição.
Exemplo vazio (mantido aqui no README, e não em `.json`, para não aparecer na lista de arquivos importáveis):

```json
{"playlists": [{"order": 1, "name": "", "description": "", "genres": [], "url": "", "icon": "🎧"}]}
```

O formato antigo `{"nome": {"url": "...", "description": "..."}}` também é aceito.

| Campo | Tipo | Descrição |
| :--- | :--- | :--- |
| `order` | número | Posição da playlist na exibição (1 a 9999). |
| `name` | texto | Nome da playlist (2 a 32 caracteres). |
| `description` | texto | Descrição curta: vazia ou de 3 a 250 caracteres. |
| `genres` | lista de textos | Até 8 gêneros musicais, de 2 a 32 caracteres, sem repetir. |
| `url` | texto | Link público da playlist (10 a 250 caracteres). Parâmetros de rastreio como `si` e `utm_*` são removidos ao gravar. |
| `icon` | texto | Opcional. Ícone da playlist: UM emoji Unicode (até 16 pontos de código; vale sequência com ZWJ ou VS16, como `👮‍♂️` e `⛏️`; não vale emoji personalizado `<:nome:id>`, texto nem mais de um emoji). Aparece como o emoji da opção no menu do `/play` (sem ele, vale o da plataforma) e antes do nome nas listas. Valor inválido é descartado (a playlist entra sem ícone), nunca barra o item. |

## Como a importação funciona

- O arquivo é validado antes de qualquer gravação (formato, tipos e tamanhos); arquivo inválido não grava nada.
- A importação só acrescenta: nunca altera nem remove playlists que o servidor já tem.
- Não duplica: playlist com o mesmo nome (sem diferenciar maiúsculas) ou o mesmo link de uma já existente é ignorada, e o
  relatório da resposta diz o motivo de cada item ignorado.
- Cada servidor guarda no máximo 25 playlists; as que passarem disso são ignoradas.
- Os dados ficam só no banco, por servidor; não há variável de ambiente para isso.
