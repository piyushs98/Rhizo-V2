"""
Walk-forward backtest package.

Invariant: at simulated time T the strategy may only observe bars with ts <= T.
Production scoring, risk, exit_rules, and PaperBroker are used — not forks.
"""
