"""Geração de tokens RPC compartilhada pelo comando e pelo painel."""

import secrets
import string

TOKEN_LENGTH = 50
TOKEN_ALPHABET = string.ascii_letters + string.digits


def generate_rpc_token() -> str:
    return "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(TOKEN_LENGTH))
