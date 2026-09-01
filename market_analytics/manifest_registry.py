"""Registro dos manifestos versionados conhecidos (DEV-004 — C3.1).

`manifest.py` permanece um contrato genérico e não sabe nada sobre manifestos
específicos. Este módulo é a única ponte entre um `manifest_id` conhecido e o
arquivo JSON correspondente em `market_analytics/manifests/` — usado pela GUI
(`tools/manifest_backfill_gui.py`) para oferecer apenas "abrir manifesto",
nunca um campo livre de caminho, símbolo ou fonte.

Continua puro: não importa `MetaTrader5`, `tkinter` nem Qt.
"""

from __future__ import annotations

from pathlib import Path

from .manifest import BackfillManifest, load_manifest_file

MANIFESTS_DIR = Path(__file__).resolve().parent / "manifests"

# Único manifesto aprovado nesta ordem (DEV-004): C3-WIN da Clear, em
# `planning`. Um manifesto futuro da FOT ou de outro ativo exige nova entrada
# aqui e nova aprovação — nunca herda a autorização deste.
KNOWN_MANIFESTS: dict[str, Path] = {
    "c3-win-clear": MANIFESTS_DIR / "c3_win_clear.json",
}


class UnknownManifestError(KeyError):
    """`manifest_id` não está no registro de manifestos conhecidos."""


class ManifestRegistryIntegrityError(RuntimeError):
    """O arquivo apontado pelo registro tem um `manifest_id` diferente da chave.

    Sinal de um `KNOWN_MANIFESTS` mal configurado (arquivo errado, cópia
    desatualizada) ou de um arquivo editado à mão -- nunca devolvido
    silenciosamente como se fosse o manifesto pedido (DEV-004, 4ª auditoria
    Codex).
    """


def known_manifest_ids() -> tuple[str, ...]:
    return tuple(sorted(KNOWN_MANIFESTS))


def load_known_manifest(manifest_id: str) -> BackfillManifest:
    path = KNOWN_MANIFESTS.get(manifest_id)
    if path is None:
        raise UnknownManifestError(f"manifesto desconhecido: {manifest_id!r}")
    manifest = load_manifest_file(path)
    if manifest.manifest_id != manifest_id:
        raise ManifestRegistryIntegrityError(
            f"registro inconsistente: {manifest_id!r} aponta para {path}, cujo manifest_id "
            f"é {manifest.manifest_id!r}"
        )
    return manifest
