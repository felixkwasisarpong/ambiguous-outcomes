"""Persistent dispatch evidence for finite-key recovery; single controller only."""
from durable import Exhausted, Journal


class RecoveryJournal(Journal):
    def __init__(self, path, crash=lambda point: None):
        super().__init__(path, crash)
        self.db.execute('''CREATE TABLE IF NOT EXISTS recovery_windows(
            operation TEXT PRIMARY KEY, strategy TEXT NOT NULL,
            epoch_at INTEGER, last_dispatch_at INTEGER)''')

    def prepare_strategy(self, operation, request, contract, deadline, max_calls, strategy):
        row = self.prepare(operation, request, contract, deadline, max_calls)
        evidence = self.window(operation)
        if evidence is None:
            if row['submitted_at'] is not None:
                raise ValueError('Cannot infer missing dispatch history')
            with self.db:
                self.db.execute('INSERT INTO recovery_windows VALUES(?,?,NULL,NULL)', (operation, strategy))
        elif evidence['strategy'] != strategy:
            raise ValueError('Persisted strategy mismatch')
        return row

    def window(self, operation):
        return self.db.execute('SELECT * FROM recovery_windows WHERE operation=?', (operation,)).fetchone()

    def reserve_create(self, operation, now, new_epoch=False):
        """Reserve a call and mark possible dispatch in ONE transaction.

        A fresh epoch is allowed by the policy only on the first request or after
        settled negative readback. The old key, original submission time and all
        limits remain unchanged. A crash can waste a reservation, never erase it.
        """
        evidence = self.window(operation)
        if evidence is None or (evidence['epoch_at'] is None and not new_epoch):
            raise ValueError('Missing dispatch evidence')
        if evidence['last_dispatch_at'] is not None and now < evidence['last_dispatch_at']:
            raise ValueError('Clock moved backwards')
        with self.db:
            changed = self.db.execute('''UPDATE operations SET used=used+1,
                submitted_at=COALESCE(submitted_at,?)
                WHERE operation=? AND used<max_calls AND ?>=0 AND ?<deadline''',
                (now, operation, now, now))
            if not changed.rowcount:
                raise Exhausted('Persistent budget or deadline exhausted')
            self.db.execute('''UPDATE recovery_windows SET epoch_at=?,last_dispatch_at=?
                WHERE operation=?''',
                (now if new_epoch else evidence['epoch_at'], now, operation))
            self.crash('during_attempt_reservation')
