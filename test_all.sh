# PYTHONWARNINGS=ignore suppresses SQLAlchemy and other warnings.
# --nocapture lets you output to stdout, while the tests is still running (arguably more useful while debugging a single test, not running the whole batch).
# --detailed-errors attempts a more detailed stack trace, but doesn't actually work.  Comment here to serve as warning.
# Also check out: --failed  (Run the tests that failed in the last test run.)  It's helpful when isolating the few tests you need to pay attention to after a full test suite run.

PYTHONWARNINGS=ignore nosetests --nocapture 




