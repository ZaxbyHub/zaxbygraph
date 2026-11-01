"""Proxy parse-error naming acceptance check (issue #48 AC4).

New file for the 40-48-36-followups trace. The DISCRIMINATING check
``test_parse_error_names_proxy_env`` (48-AC4: when the origin remote fails
to parse because ZAXBYGRAPH_PROXY_FORGE_HOST disabled the loopback rewrite,
the parse-error message names the variable and its disabled state) is
authored by the fix, not at freeze time: at the frozen base the parse error
blames the remote URL only (reproduced in 02-reproduction.md, probe P3), so
the absence leg observes ``NO TESTS RAN`` (exit 5 on py -3.14).
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from zaxbygraph.repo import (
    RepoError,
    _proxy_disabled_reason,
    remote_info,
    slug_from_git,
)

LOOPBACK_URL = "http://user@127.0.0.1:41233/git/owner/repo"


class ProxyDisabledReasonUnitTests(unittest.TestCase):
    """The counterfactual gate: only a would-have-parsed loopback git/ URL
    in a disabled env state names the variable."""

    def test_disabled_states_name_the_variable(self):
        for value in ("", "   ", "!!not a host!!", "a..b"):
            with self.subTest(value=value):
                with mock.patch.dict(
                    os.environ, {"ZAXBYGRAPH_PROXY_FORGE_HOST": value}
                ):
                    reason = _proxy_disabled_reason(LOOPBACK_URL)
                self.assertIsNotNone(reason, value)
                self.assertIn("ZAXBYGRAPH_PROXY_FORGE_HOST", reason)

    def test_unset_and_valid_states_never_attribute(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(
                _proxy_disabled_reason(LOOPBACK_URL),
                "the unset state is not a disabled state",
            )
        for value in ("github.com", "gitlab.internal:8443"):
            with mock.patch.dict(
                os.environ, {"ZAXBYGRAPH_PROXY_FORGE_HOST": value}
            ):
                self.assertIsNone(
                    _proxy_disabled_reason(LOOPBACK_URL),
                    f"valid-host state {value!r} is not a disabled state",
                )

    def test_non_counterfactual_urls_get_no_attribution(self):
        # Would fail to parse even with the rewrite enabled: no scheme,
        # no git/ prefix, malformed slug, non-loopback authority.
        cases = [
            "git@127.0.0.1:owner/repo",          # scp form: no rewrite path
            "http://127.0.0.1:1/owner/repo",     # no git/ prefix
            "http://127.0.0.1:1/git/bad slug!",  # malformed slug
            "http://example.com:1/git/owner/repo",  # not loopback
        ]
        with mock.patch.dict(
            os.environ, {"ZAXBYGRAPH_PROXY_FORGE_HOST": ""}
        ):
            for url in cases:
                with self.subTest(url=url):
                    self.assertIsNone(_proxy_disabled_reason(url))


class ParseErrorNamingTests(unittest.TestCase):
    """48-AC4 end-to-end: both public callers append the reason."""

    def test_parse_error_names_proxy_env(self):
        def fake_run(argv, **kw):
            class P:
                returncode = 0
                stdout = LOOPBACK_URL + "\n"
                stderr = ""

            return P()

        for caller in (slug_from_git, remote_info):
            with self.subTest(caller=caller.__name__):
                with mock.patch.dict(
                    os.environ, {"ZAXBYGRAPH_PROXY_FORGE_HOST": "   "}
                ):
                    with mock.patch(
                        "zaxbygraph.repo.subprocess.run", side_effect=fake_run
                    ):
                        with self.assertRaises(RepoError) as ctx:
                            caller()
                msg = str(ctx.exception)
                self.assertIn("could not parse origin remote", msg)
                self.assertIn("ZAXBYGRAPH_PROXY_FORGE_HOST", msg)
                self.assertIn("disables the loopback rewrite", msg)


if __name__ == "__main__":
    unittest.main()
