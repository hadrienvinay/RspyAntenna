╔══════════════════════════════════════════════════════╗
║        Ground Station Monitor — Installation         ║
║        Raspberry Pi 3 · RTL-SDR V4 · Arcachon        ║
╚══════════════════════════════════════════════════════╝

1. COPIER LES FICHIERS
   scp ground-station-final/* suri@192.168.1.20:/home/suri/monitor/

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

6. FICHIERS
   monitor.py           → serveur principal (HTTP + WebSocket)
   dashboard.html       → interface web
   noaa_tracker.py      → réception satellites météo NOAA
   space_tracker.py     → ISS live + prédiction passages + météo spatiale
   gnss-sdr-rtlsdr.conf → config récepteur Galileo/GPS
   monitor.service      → service systemd autostart

OBSERVATEUR : Arcachon (44.6611°N, 1.1638°W)
