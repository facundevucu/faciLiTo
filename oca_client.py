import os
import time
import logging
import requests

log = logging.getLogger("asistente-lito")


class OCAClient:
    def __init__(self):
        self.base_url  = os.environ["OCA_API_URL"].rstrip("/")
        self.id_key    = os.environ["OCA_ID_KEY"]
        self.secret_key = os.environ["OCA_SECRET_KEY"]
        self.rut       = os.environ["OCA_RUT"]
        self._token: str | None = None
        self._token_ts: float   = 0

    def _token_valid(self) -> bool:
        if not self._token:
            return False
        return (time.time() - self._token_ts) < 270  # renew with 30s margin

    def signin(self) -> str:
        resp = requests.post(
            f"{self.base_url}/api/signin",
            json={"id_key": self.id_key, "secret_key": self.secret_key},
            timeout=15,
        )
        resp.raise_for_status()
        self._token    = resp.json()["data"]["id_token"]
        self._token_ts = time.time()
        log.info("OCA: token renovado")
        return self._token

    def _get_token(self) -> str:
        if not self._token_valid():
            self.signin()
        return self._token

    def get_pagos(self, start_date: str, end_date: str, **filters) -> list[dict]:
        """Fetch all pagos for the date range, handling pagination and token expiry."""
        params = {"start_date": start_date, "end_date": end_date}
        params.update({k: v for k, v in filters.items() if v is not None})

        all_pagos: list[dict] = []
        token_renewed = False

        while True:
            headers = {"Authorization": f"Bearer {self._get_token()}"}
            resp = requests.get(
                f"{self.base_url}/api/companies/{self.rut}/payout",
                headers=headers,
                params=params,
                timeout=15,
            )

            if resp.status_code == 401 and not token_renewed:
                log.warning("OCA: 401 recibido — renovando token y reintentando")
                self._token = None
                token_renewed = True
                all_pagos = []
                params.pop("scroll_id", None)
                continue

            resp.raise_for_status()
            data  = resp.json()
            pagos = data.get("pagos", [])
            all_pagos.extend(pagos)

            scroll_id = data.get("scroll_id")
            if not scroll_id or not pagos:
                break
            params["scroll_id"] = scroll_id

        return all_pagos
