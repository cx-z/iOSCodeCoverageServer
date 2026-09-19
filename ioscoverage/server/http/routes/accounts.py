"""Platform registration, Session, and administrator account routes."""

from datetime import datetime
from email.utils import format_datetime
from http import HTTPStatus

from ioscoverage.storage.accounts import AccountStoreError
from ioscoverage.storage.coverage_store import CoverageStoreError


class AccountRoutesMixin:
    """Maps account-domain operations to bounded JSON and HttpOnly Cookie responses."""

    def _handle_auth_register(self) -> None:
        """Registers one developer and creates its initial fixed-lifetime Session."""

        try:
            payload = self._read_exact_json({"schema_version", "username", "password"})
            store = self._account_store()
            user = store.create_user(payload["username"], payload["password"], "developer")
            session = store.create_session(str(user["id"]))
        except (AccountStoreError, CoverageStoreError, KeyError) as error:
            self._send_account_error(error)
            return
        self._send_json(
            HTTPStatus.CREATED,
            {"status": "registered", "user": user},
            extra_headers={"Set-Cookie": self._session_cookie(session)},
        )

    def _handle_auth_login(self) -> None:
        """Authenticates one active account and establishes a new browser Session."""

        try:
            payload = self._read_exact_json({"schema_version", "username", "password"})
            store = self._account_store()
            user = store.authenticate(payload["username"], payload["password"])
            if user is None:
                raise AccountStoreError("invalid_credentials", "username or password is invalid")
            session = store.create_session(str(user["id"]))
        except (AccountStoreError, CoverageStoreError, KeyError) as error:
            self._send_account_error(error)
            return
        self._send_json(
            HTTPStatus.OK,
            {"status": "authenticated", "user": user},
            extra_headers={"Set-Cookie": self._session_cookie(session)},
        )

    def _handle_auth_session(self, principal) -> None:
        """Returns the current safe account projection without Session material."""

        try:
            user = self._account_store().get_user(str(principal.user_id))
        except AccountStoreError as error:
            self._send_account_error(error)
            return
        self._send_json(HTTPStatus.OK, {"user": user})

    def _handle_auth_logout(self) -> None:
        """Invalidates only the current Session and clears its browser Cookie."""

        store = self._account_store()
        store.logout(self._session_cookie_token())
        self._send_json(
            HTTPStatus.OK,
            {"status": "logged_out"},
            extra_headers={"Set-Cookie": self._clear_session_cookie()},
        )

    def _handle_admin_user_list(self) -> None:
        """Returns all safe user projections to an authorized administrator."""

        try:
            users = self._account_store().list_users()
        except AccountStoreError as error:
            self._send_account_error(error)
            return
        self._send_json(HTTPStatus.OK, {"users": users, "count": len(users)})

    def _handle_admin_user_create(self) -> None:
        """Creates one developer or administrator from an exact administrator payload."""

        try:
            payload = self._read_exact_json(
                {"schema_version", "username", "password", "role"}
            )
            user = self._account_store().create_user(
                payload["username"], payload["password"], payload["role"]
            )
        except (AccountStoreError, CoverageStoreError, KeyError) as error:
            self._send_account_error(error)
            return
        self._send_json(HTTPStatus.CREATED, {"status": "created", "user": user})

    def _handle_admin_user_status(self, user_id: str) -> None:
        """Activates or disables one account using the AccountStore transaction."""

        try:
            payload = self._read_exact_json({"schema_version", "status"})
            user = self._account_store().set_status(user_id, payload["status"])
        except (AccountStoreError, CoverageStoreError, KeyError) as error:
            self._send_account_error(error)
            return
        self._send_json(HTTPStatus.OK, {"status": "updated", "user": user})

    def _handle_admin_user_reset_password(self, user_id: str) -> None:
        """Replaces one password and revokes every existing Session for that account."""

        try:
            payload = self._read_exact_json({"schema_version", "password"})
            user = self._account_store().reset_password(user_id, payload["password"])
        except (AccountStoreError, CoverageStoreError, KeyError) as error:
            self._send_account_error(error)
            return
        self._send_json(HTTPStatus.OK, {"status": "password_reset", "user": user})

    def _read_exact_json(self, fields: set):
        """Requires schema version 1 and exactly the fields defined for one account API."""

        payload = self._read_json_body()
        if set(payload) != fields:
            raise CoverageStoreError("invalid_fields", "request fields are invalid")
        schema_version = payload.get("schema_version")
        if schema_version != 1 or isinstance(schema_version, bool):
            raise CoverageStoreError("invalid_schema_version", "schema_version must be 1")
        return payload

    def _account_store(self):
        """Returns the startup-owned account store or a bounded availability error."""

        store = self.config.account_store
        if store is None:
            raise AccountStoreError("account_store_unavailable", "account service is unavailable")
        return store

    def _session_cookie(self, session) -> str:
        """Builds the fixed HttpOnly Strict Cookie without exposing it in JSON or logs."""

        expires_at = datetime.fromisoformat(str(session["expires_at"]))
        return (
            "ios_coverage_session={}; Path=/; Expires={}; HttpOnly; SameSite=Strict".format(
                session["token"], format_datetime(expires_at, usegmt=True)
            )
        )

    def _clear_session_cookie(self) -> str:
        """Builds an immediate-expiry Cookie for browser logout."""

        return (
            "ios_coverage_session=; Path=/; Max-Age=0; "
            "Expires=Thu, 01 Jan 1970 00:00:00 GMT; HttpOnly; SameSite=Strict"
        )
