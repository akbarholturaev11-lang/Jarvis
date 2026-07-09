import subprocess
import sys

from core.i18n import t


print(t("setup.install_requirements"))
subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], check=True)

print(t("setup.install_playwright"))
subprocess.run([sys.executable, "-m", "playwright", "install"], check=True)

print(f"\n✅ {t('setup.complete')}")

