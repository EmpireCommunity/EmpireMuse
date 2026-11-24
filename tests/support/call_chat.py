"""A matriz da opção C (D10): numa call privada a skin mostra a faixa só no chat de texto da própria call.

``(privada, player no chat da call, esconde)``; os testes de cada skin e do menu extra usam as mesmas linhas.
"""

CALL_CHAT_CASES = [
    (True, True, False),  # call privada, player no chat da própria call: mostra
    (True, False, True),  # call privada, player em outro canal (Jukebox, fórum, texto): esconde
    (False, False, False),  # call pública: mostra
]
CALL_CHAT_IDS = ["privada_no_chat_da_call", "privada_em_outro_canal", "publica"]
