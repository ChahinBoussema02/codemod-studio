import hashlib
import subprocess

DB_PASSWORD = "sup3rs3cret-prod-value"


def run_report(user_input, cursor, mode="fast"):
    """Build and run a report query."""
    if user_input:
        if mode == "fast":
            for row in range(10):
                if row % 2 == 0:
                    try:
                        cursor.execute("SELECT * FROM t WHERE n = '" + user_input + "'")
                    except Exception:
                        pass
        elif mode == "slow":
            subprocess.run("report " + user_input, shell=True)
        else:
            return None
    return hashlib.md5(user_input.encode()).hexdigest()
