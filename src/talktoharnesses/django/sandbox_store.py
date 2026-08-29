"""Django-backed persistence for the sandboxes the proxy spawns."""

from __future__ import annotations

from asgiref.sync import sync_to_async
from tth_types.enums import HarnessKind

from talktoharnesses.django.models import SandboxRecord
from talktoharnesses.remote.sandbox import SandboxRecordData


def _to_data(row: SandboxRecord) -> SandboxRecordData:
    return SandboxRecordData(
        kind=HarnessKind(row.kind),
        container_name=row.container_name,
        image=row.image,
        host_port=row.host_port,
        base_url=row.base_url,
        split_token=row.split_token,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_ready_at=row.last_ready_at,
    )


class DjangoSandboxStore:
    """Implements the ``SandboxStore`` protocol over ``SandboxRecord`` rows."""

    async def get(self, kind: HarnessKind) -> SandboxRecordData | None:
        return await sync_to_async(self._get, thread_sensitive=True)(kind)

    def _get(self, kind: HarnessKind) -> SandboxRecordData | None:
        row = SandboxRecord.objects.filter(kind=kind.value).first()
        if row is None:
            return None
        return _to_data(row)

    async def upsert(self, record: SandboxRecordData) -> None:
        await sync_to_async(self._upsert, thread_sensitive=True)(record)

    def _upsert(self, record: SandboxRecordData) -> None:
        SandboxRecord.objects.update_or_create(
            kind=record.kind.value,
            defaults={
                "container_name": record.container_name,
                "image": record.image,
                "host_port": record.host_port,
                "base_url": record.base_url,
                "split_token": record.split_token,
                "status": record.status,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
                "last_ready_at": record.last_ready_at,
            },
        )

    async def reserve(self, record: SandboxRecordData) -> SandboxRecordData:
        return await sync_to_async(self._reserve, thread_sensitive=True)(record)

    def _reserve(self, record: SandboxRecordData) -> SandboxRecordData:
        # kind is the primary key, so concurrent workers racing to create the
        # row resolve inside get_or_create: exactly one insert wins and the
        # loser fetches it, which is what keeps the split token single-minted.
        row, _created = SandboxRecord.objects.get_or_create(
            kind=record.kind.value,
            defaults={
                "container_name": record.container_name,
                "image": record.image,
                "host_port": record.host_port,
                "base_url": record.base_url,
                "split_token": record.split_token,
                "status": record.status,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
                "last_ready_at": record.last_ready_at,
            },
        )
        return _to_data(row)
