#!/bin/bash
# Pornește botul și ține Mac-ul treaz, chiar și cu capacul închis, cât timp merge botul.
# Rulezi:  ./porneste.sh     Oprești:  Control+C

cd "$(dirname "$0")"
source .venv/bin/activate

echo "Îți cer parola Mac-ului ca să nu adoarmă cu capacul închis."
sudo pmset -a disablesleep 1

# când oprești botul (Control+C sau închizi terminalul), Mac-ul poate adormi din nou
reda_somnul() {
    sudo pmset -a disablesleep 0
    echo "Gata. Mac-ul poate adormi din nou normal."
}
trap reda_somnul EXIT

# caffeinate ține Mac-ul treaz cât timp merge botul
caffeinate -i python bot.py