import adapters
import core
import runtime.runner as runner
import transport.telegram as telegram
import web.dashboard as dashboard


def test_module_packages_expose_expected_symbols():
    assert adapters.Binance
    assert core.Quote
    assert runner.run_once
    assert telegram.tg_send_message
    assert dashboard.serve_http
