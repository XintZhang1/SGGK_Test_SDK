"""Reviewed NXOpen journal that verifies access to the current NX Session."""

import json

import NXOpen


def main() -> None:
    session = NXOpen.Session.GetSession()
    print(
        json.dumps(
            {
                "kind": "sggk_nx_session_smoke",
                "ok": session is not None,
                "session_type": type(session).__name__,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
