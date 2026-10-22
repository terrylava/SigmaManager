"""dzsm — DayZ server manager.

Design spec: ../dayz-server-manager-spec.md
"""

# The one place the version is written. pyproject reads it from here, and a test
# fails when the two disagree -- this said 0.1.0 while the newest tag was v0.1.1,
# which an updater comparing its own version against a release would have believed.
__version__ = "0.1.1"
