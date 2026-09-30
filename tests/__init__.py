import os

# Hermetic (v5 power spacing): no test waits on a real gNB's last RF write, and one test's
# power write never makes the next one sleep.  tests/test_v5_power_spacing.py sets a path.
os.environ["AIC_POWER_WRITE_STAMP"] = ""
