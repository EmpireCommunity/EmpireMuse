"""T-533a: ``TokenProof`` (a leitura da prova) isolado: só IDs que autenticam, sem vazar a causa, com teto e sem deixar o contador preso."""

import asyncio

import pytest

from empiremuse.utils.rpc_proof import ProofUnavailable, TokenProof

pytestmark = [pytest.mark.critical, pytest.mark.regression]

TOKEN = "a" * 50


class _Banco:
    def __init__(self, docs=None, erro=None, gate=None):
        self.docs, self.erro, self.gate, self.calls = docs or {}, erro, gate, []

    async def find_doc(self, id_, *, db_name, collection):
        self.calls.append((id_, db_name, collection))
        if self.gate is not None:
            await self.gate.wait()
        if self.erro is not None:
            raise self.erro
        return self.docs.get(id_)


@pytest.mark.asyncio
async def test_only_ids_whose_stored_token_matches_are_returned():
    banco = _Banco({1: {"token": TOKEN}, 2: {"token": "b" * 50}, 3: {"token": ""}, 4: {}, 5: "nao-e-dict"})

    verified = await TokenProof(lambda: banco).verify([1, 2, 3, 4, 5, 6], TOKEN)

    assert verified == frozenset({1})
    assert {c[1:] for c in banco.calls} == {("users", "global")}


@pytest.mark.asyncio
async def test_an_empty_sent_token_never_verifies_even_against_an_empty_stored_token():
    assert await TokenProof(lambda: _Banco({1: {"token": ""}})).verify([1], "") == frozenset()


@pytest.mark.asyncio
async def test_a_missing_database_is_unavailable_without_querying():
    proof = TokenProof(lambda: None)

    with pytest.raises(ProofUnavailable) as caught:
        await proof.verify([1], TOKEN)

    assert caught.value.args == ("NoDatabase",)


@pytest.mark.asyncio
async def test_an_error_becomes_unavailable_carrying_only_the_type_name_never_the_message():
    banco = _Banco(erro=RuntimeError("mongodb://usuario:senha-secreta@host"))

    with pytest.raises(ProofUnavailable) as caught:
        await TokenProof(lambda: banco).verify([1], TOKEN)

    assert caught.value.args == ("RuntimeError",)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert "senha-secreta" not in repr(caught.value)


@pytest.mark.asyncio
async def test_a_failure_in_the_database_getter_is_unavailable_too():
    def quebrado():
        raise LookupError("segredo")

    with pytest.raises(ProofUnavailable) as caught:
        await TokenProof(quebrado).verify([1], TOKEN)

    assert caught.value.args == ("LookupError",)


@pytest.mark.asyncio
async def test_a_timeout_is_unavailable_and_releases_the_in_flight_slot():
    proof = TokenProof(
        lambda: _Banco(gate=asyncio.Event()), timeout=0
    )  # nunca liberado: esgota na próxima volta do loop

    with pytest.raises(ProofUnavailable) as caught:
        await proof.verify([1], TOKEN)

    assert caught.value.args == ("TimeoutError",) and proof.in_flight == 0


@pytest.mark.asyncio
async def test_the_in_flight_cap_refuses_without_querying_and_counts_per_registration_not_per_id():
    gate = asyncio.Event()
    banco = _Banco({1: {"token": TOKEN}, 2: {"token": TOKEN}, 3: {"token": TOKEN}}, gate=gate)
    proof = TokenProof(lambda: banco, max_in_flight=1)
    primeira = asyncio.ensure_future(proof.verify([1, 2, 3], TOKEN))  # três IDs, um só registro em voo
    async with asyncio.timeout(2):
        while len(banco.calls) < 3:
            await asyncio.sleep(0)
    assert proof.in_flight == 1

    with pytest.raises(ProofUnavailable) as caught:
        await proof.verify([4], TOKEN)
    assert caught.value.args == ("InFlightLimit",) and len(banco.calls) == 3  # o excedente não consultou

    gate.set()
    assert await primeira == frozenset({1, 2, 3}) and proof.in_flight == 0


@pytest.mark.asyncio
async def test_cancellation_passes_through_and_releases_the_slot():
    proof = TokenProof(lambda: _Banco(gate=asyncio.Event()))
    task = asyncio.ensure_future(proof.verify([1], TOKEN))
    async with asyncio.timeout(2):
        while proof.in_flight == 0:
            await asyncio.sleep(0)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert proof.in_flight == 0
