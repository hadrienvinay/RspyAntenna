╔══════════════════════════════════════════════════════╗
║        Ground Station Monitor — Installation         ║
║        Raspberry Pi 3 · RTL-SDR V4 · Arcachon        ║
╚══════════════════════════════════════════════════════╝
ssh suri@192.168.1.20

1. COPIER LES FICHIERS
   scp ground-station-final/* suri@192.168.1.20:/home/suri/monitor/

   Ou, depuis ce repo, pour redéployer après une modification :
   ./deploy.sh
   (copie les fichiers .py/.html modifiés + redémarre le service monitor
    — variables PI_USER/PI_HOST/PI_DIR surchargeables, voir le script)

2. DÉPENDANCES
   sudo apt install -y rtl-sdr sox gnss-sdr python3-pip
   pip3 install psutil websockets --break-system-packages

   # noaa-apt (décodeur images satellites)
   wget https://github.com/martinber/noaa-apt/releases/download/v1.4.0/noaa-apt-1.4.0-aarch64-linux-gnu-nogui.zip
   unzip noaa-apt-*.zip
   sudo cp noaa-apt-*/noaa-apt /usr/local/bin/
   sudo chmod +x /usr/local/bin/noaa-apt

   # dump1090-fa (ADS-B, optionnel)
   curl -sSL https://flightaware.com/adsb/piaware/files/packages/pool/piaware/f/flightaware-apt-repository/flightaware-apt-repository_1.1_all.deb -o /tmp/fa.deb
   sudo dpkg -i /tmp/fa.deb && sudo apt update
   sudo apt install -y dump1090-fa

3. SERVICE SYSTEMD
   sudo cp /home/suri/monitor/monitor.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable monitor
   sudo systemctl start monitor

4. ACCÈS
   Dashboard  → http://192.168.1.20:8888
   ADS-B      → http://192.168.1.20:8080

5. GNSS-SDR (Galileo)
   # Arrêter dump1090 d'abord
   sudo systemctl stop dump1090-fa
   cd /home/suri/monitor
   gnss-sdr --config_file=gnss-sdr-rtlsdr.conf

   Ou depuis le dashboard → bouton DÉMARRER dans la section GNSS

6. BOUTON REBOOT (dashboard → header)
   Le dashboard peut redémarrer le Raspberry Pi via sudo.
   Autoriser suri à exécuter reboot sans mot de passe :

   sudo visudo -f /etc/sudoers.d/monitor-reboot
   # Ajouter la ligne :
   suri ALL=(ALL) NOPASSWD: /sbin/reboot

   Sans cette règle, le bouton REBOOT échouera (mot de passe sudo requis).

7. DEPLOY.SH (redémarrage du service via sudo)
   ./deploy.sh copie les fichiers puis exécute à distance :
     sudo systemctl restart monitor
     sudo systemctl status monitor --no-pager -l
   Ces commandes tournent en SSH non-interactif (pas de terminal pour
   saisir le mot de passe) — il faut donc aussi une règle NOPASSWD :

   sudo visudo -f /etc/sudoers.d/monitor-deploy
   # Ajouter la ligne :
   suri ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart monitor, /usr/bin/systemctl status monitor *, /usr/bin/systemctl stop monitor, /usr/bin/systemctl start monitor

   (le "*" après "status monitor" est nécessaire car deploy.sh passe des
   arguments supplémentaires --no-pager -l ; sudoers compare la commande
   littéralement). Vérifier le chemin de systemctl avec `which systemctl`
   si différent de /usr/bin/systemctl.

   Sans cette règle, ./deploy.sh échoue avec :
   "sudo: a terminal is required to read the password" ou
   "sudo: a password is required".

8. LOGS EN DIRECT (dashboard → onglet Logs)
   Diffusés via journalctl -u monitor -f (WebSocket port 8767).
   Fonctionne uniquement quand monitor tourne comme service systemd
   (voir §3) — journalctl doit pouvoir lire les logs du service.

9. FICHIERS
   monitor.py           → serveur principal (HTTP + WebSocket)
   dashboard.html       → interface web
   noaa_tracker.py      → réception satellites météo NOAA
   space_tracker.py     → ISS live + prédiction passages + météo spatiale
   gnss-sdr-rtlsdr.conf → config récepteur Galileo/GPS
   monitor.service      → service systemd autostart

OBSERVATEUR : Arcachon (44.6611°N, 1.1638°W)
