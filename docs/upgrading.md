# Forward upgrade guide

Conservative stop/migrate/start procedure for the first stable release. Mixed-version
rolling upgrades are not supported. Backward migration compatibility is not promised.

## Procedure

1. Read the target [`SUPPORTED_HARNESSES.md`](../SUPPORTED_HARNESSES.md) and release notes.
   Verify provider versions and platform rows before changing the package.
2. Back up the relational database and retain the currently installed artifact and
   host configuration.
3. Stop all old service processes cleanly and verify they are no longer ready
   (`/api/v1/ready` fails closed or the process is gone).
4. Install the exact new wheel with the same required extras from a
   lock/constraints-controlled host environment.
5. Run migrations once before starting new workers:

   ```bash
   python manage.py migrate
   ```

   The package never runs migrations automatically.
6. Start the lifespan-wrapped ASGI service, wait for `/api/v1/ready`, probe configured
   harnesses, and run a harmless owner-scoped smoke conversation before restoring traffic.
7. Run the externally scheduled retention command only after the upgraded service is healthy:

   ```bash
   python manage.py talktoharnesses_cleanup
   ```

## Rollback

After a forward migration, rollback means:

1. Stop the new processes.
2. Restore the pre-upgrade database backup together with the old artifact.
3. Start the previous version.

Do not reverse migrations against production data.
