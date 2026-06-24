# Fix for PyQt5 DLL loading on Windows with PyInstaller
import os
import sys

if getattr(sys, 'frozen', False):
    # Running as bundled exe
    base_path = sys._MEIPASS
    qt_bin = os.path.join(base_path, 'PyQt5', 'Qt5', 'bin')
    if os.path.exists(qt_bin):
        os.environ['PATH'] = qt_bin + os.pathsep + os.environ.get('PATH', '')
        try:
            os.add_dll_directory(qt_bin)
        except Exception:
            pass

#!/usr/bin/env python3
"""
WiFi Hotspot Controller with Approval System (PyQt5 version)
Run as Administrator on Windows 7/10/11.
"""

import subprocess
import time
import threading
import re
from datetime import datetime, timedelta
import sys
import os

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QSpinBox, QGroupBox, QTreeWidget,
    QTreeWidgetItem, QHeaderView, QMessageBox, QInputDialog, QStatusBar,
    QFormLayout
)
from PyQt5.QtCore import QThread, pyqtSignal, Qt, QTimer


# ---------------------- Hotspot engine ----------------------
class HotspotManager:
    def __init__(self):
        self.ssid = "MyHotspot"
        self.password = "StrongPass123"
        self.max_clients = 10
        self.connected_devices = {}
        self.blocked_devices = set()
        self.running = False
        self.interface_name = None
        self.internet_adapter = None

    def find_adapters(self):
        try:
            output = subprocess.check_output("netsh wlan show hostednetwork", shell=True, text=True)
            match = re.search(r"Interface name\s*:\s*(.+)", output)
            if match:
                self.interface_name = match.group(1).strip()
            else:
                raise Exception("Could not find hosted network interface. Is your Wi-Fi adapter supported?")

            ipconfig = subprocess.check_output("ipconfig", shell=True, text=True)
            adapter_sections = ipconfig.split("\r\n\r\n")
            for sec in adapter_sections:
                if "Default Gateway" in sec and " 0.0.0.0" not in sec:
                    adapter_line = sec.splitlines()[0].strip()
                    if adapter_line.endswith(':'):
                        adapter_name = adapter_line[:-1]
                    else:
                        adapter_name = adapter_line
                    if adapter_name != self.interface_name:
                        self.internet_adapter = adapter_name
                        return
            raise Exception("Could not auto-detect internet adapter.")
        except subprocess.CalledProcessError as e:
            raise Exception(f"Failed to run netsh. Are you running as Administrator? {e}")

    def enable_ics(self):
        ps_script = f'''
        $public = Get-NetAdapter -Name "{self.internet_adapter}"
        $private = Get-NetAdapter -Name "{self.interface_name}"
        if (!$public -or !$private) {{
            Write-Error "Adapters not found"
            return
        }}
        $mpublic = Get-WmiObject -Class Win32_NetworkAdapter -Filter "NetConnectionID='{self.internet_adapter}'"
        $mprivate = Get-WmiObject -Class Win32_NetworkAdapter -Filter "NetConnectionID='{self.interface_name}'"
        if ($mpublic -and $mprivate) {{
            $mpublic.EnableStatic(0, "192.168.137.1", "255.255.255.0")
            $sharing = $mpublic.EnableSharing(0)
        }} else {{
            $mgr = New-Object -ComObject HNetCfg.HNetShare
            $publicConfig = $mgr.EnumEveryConnection |? {{ $_.Name -eq "{self.internet_adapter}" }}
            $privateConfig = $mgr.EnumEveryConnection |? {{ $_.Name -eq "{self.interface_name}" }}
            $publicConfig.SharingConfiguration.EnableSharing(0, $privateConfig)
        }}
        '''
        try:
            subprocess.run(["powershell", "-Command", ps_script], capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as e:
            raise Exception(f"Could not enable ICS: {e.stderr}")

    def start_hotspot(self):
        subprocess.run(f'netsh wlan set hostednetwork mode=allow ssid={self.ssid} key={self.password} maxclients={self.max_clients}',
                       shell=True, check=True, capture_output=True)
        subprocess.run('netsh wlan start hostednetwork', shell=True, check=True, capture_output=True)
        self.running = True
        self.find_adapters()
        try:
            self.enable_ics()
        except Exception:
            pass  # user can manually share

    def stop_hotspot(self):
        subprocess.run('netsh wlan stop hostednetwork', shell=True, capture_output=True)
        self.running = False

    def set_config(self, ssid, password, max_clients):
        self.ssid = ssid
        self.password = password
        self.max_clients = max_clients
        if self.running:
            self.stop_hotspot()
            time.sleep(2)
            self.start_hotspot()

    def get_connected_clients(self):
        try:
            output = subprocess.check_output('netsh wlan show hostednetwork', shell=True, text=True)
        except subprocess.CalledProcessError:
            return []
        client_section = re.findall(r'Clients connected.*?\n(.*?)\n\n', output, re.DOTALL)
        if not client_section:
            return []
        lines = client_section[0].strip().splitlines()
        clients = []
        for line in lines:
            parts = line.strip().split(None, 2)
            if len(parts) >= 2:
                mac = parts[0].upper()
                ip = parts[1]
                hostname = parts[2] if len(parts) > 2 else "Unknown"
                clients.append((mac, ip, hostname))
        return clients

    def block_internet(self, ip_address):
        rule_name = f"HotspotBlock_{ip_address}"
        subprocess.run(
            f'netsh advfirewall firewall add rule name="{rule_name}" dir=out remoteip={ip_address} protocol=any action=block',
            shell=True, capture_output=True
        )
        return rule_name

    def unblock_internet(self, rule_name):
        subprocess.run(f'netsh advfirewall firewall delete rule name="{rule_name}"', shell=True, capture_output=True)

    def approve_device(self, mac, ip):
        dev = self.connected_devices.get(mac)
        if dev and not dev['approved']:
            self.unblock_internet(dev['block_rule_name'])
            dev['approved'] = True
            dev['block_rule_name'] = None
            return True
        return False

    def block_device(self, mac, ip):
        dev = self.connected_devices.get(mac)
        if dev:
            if dev['approved']:
                rule_name = f"HotspotBlock_{ip}"
                self.block_internet(ip)
                dev['block_rule_name'] = rule_name
            dev['approved'] = False
            self.blocked_devices.add(mac)
        else:
            self.blocked_devices.add(mac)

    def unblock_permanently(self, mac):
        self.blocked_devices.discard(mac)
        if mac in self.connected_devices:
            dev = self.connected_devices[mac]
            if not dev['approved'] and dev.get('block_rule_name'):
                self.approve_device(mac, dev['ip'])

    def set_time_limit(self, mac, minutes):
        dev = self.connected_devices.get(mac)
        if dev:
            dev['expiry'] = datetime.now() + timedelta(minutes=minutes)

    def cleanup_rules(self):
        try:
            rules = subprocess.check_output('netsh advfirewall firewall show rule name=all', shell=True, text=True)
            for line in rules.splitlines():
                if 'HotspotBlock_' in line:
                    rule_name = line.strip().split(":")[-1].strip()
                    self.unblock_internet(rule_name)
        except:
            pass


# ---------------------- Monitor Thread ----------------------
class MonitorThread(QThread):
    new_device = pyqtSignal(str)
    remove_device = pyqtSignal(str)
    update_list = pyqtSignal()
    error_occurred = pyqtSignal(str)

    def __init__(self, manager):
        super().__init__()
        self.manager = manager
        self.running = True

    def run(self):
        while self.running:
            try:
                current_clients = self.manager.get_connected_clients()
                current_macs = set()
                for mac, ip, hostname in current_clients:
                    current_macs.add(mac)
                    if mac not in self.manager.connected_devices:
                        if mac not in self.manager.blocked_devices:
                            rule_name = self.manager.block_internet(ip)
                            self.manager.connected_devices[mac] = {
                                'ip': ip,
                                'hostname': hostname,
                                'approved': False,
                                'block_rule_name': rule_name,
                                'expiry': None,
                                'first_seen': datetime.now(),
                            }
                            self.new_device.emit(mac)
                    else:
                        dev = self.manager.connected_devices[mac]
                        if dev['ip'] != ip:
                            if not dev['approved'] and dev['block_rule_name']:
                                self.manager.unblock_internet(dev['block_rule_name'])
                                dev['block_rule_name'] = self.manager.block_internet(ip)
                            dev['ip'] = ip
                        dev['hostname'] = hostname
                        # Time limit check
                        if dev['expiry'] and datetime.now() > dev['expiry']:
                            self.manager.block_device(mac, ip)
                            dev['expiry'] = None
                            self.update_list.emit()

                for mac in list(self.manager.connected_devices.keys()):
                    if mac not in current_macs:
                        dev = self.manager.connected_devices.pop(mac)
                        if not dev['approved'] and dev['block_rule_name']:
                            self.manager.unblock_internet(dev['block_rule_name'])
                        self.remove_device.emit(mac)

                self.update_list.emit()
                time.sleep(2)
            except Exception as e:
                self.error_occurred.emit(str(e))
                time.sleep(5)

    def stop(self):
        self.running = False


# ---------------------- Main Window ----------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.manager = HotspotManager()
        self.monitor = None
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle("WiFi Hotspot Controller")
        self.setMinimumSize(850, 550)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        # Config group
        config_group = QGroupBox("Hotspot Configuration")
        config_layout = QFormLayout()
        config_group.setLayout(config_layout)

        self.ssid_entry = QLineEdit("MyHotspot")
        config_layout.addRow("SSID:", self.ssid_entry)

        self.password_entry = QLineEdit("StrongPass123")
        self.password_entry.setEchoMode(QLineEdit.Password)
        config_layout.addRow("Password:", self.password_entry)

        self.max_clients_spin = QSpinBox()
        self.max_clients_spin.setRange(1, 32)
        self.max_clients_spin.setValue(10)
        config_layout.addRow("Max Clients:", self.max_clients_spin)

        btn_layout = QHBoxLayout()
        self.start_btn = QPushButton("Start Hotspot")
        self.start_btn.clicked.connect(self.start)
        btn_layout.addWidget(self.start_btn)

        self.stop_btn = QPushButton("Stop Hotspot")
        self.stop_btn.clicked.connect(self.stop)
        self.stop_btn.setEnabled(False)
        btn_layout.addWidget(self.stop_btn)

        config_layout.addRow(btn_layout)
        main_layout.addWidget(config_group)

        # Clients group
        clients_group = QGroupBox("Connected Devices")
        clients_layout = QVBoxLayout()
        clients_group.setLayout(clients_layout)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["MAC", "IP", "Hostname", "Status", "Expires"])
        self.tree.setAlternatingRowColors(True)
        self.tree.header().setSectionResizeMode(QHeaderView.Stretch)
        clients_layout.addWidget(self.tree)

        action_layout = QHBoxLayout()
        self.approve_btn = QPushButton("Approve")
        self.approve_btn.clicked.connect(self.approve)
        action_layout.addWidget(self.approve_btn)

        self.block_btn = QPushButton("Block Permanently")
        self.block_btn.clicked.connect(self.block_permanent)
        action_layout.addWidget(self.block_btn)

        self.unblock_btn = QPushButton("Unblock (Whitelist)")
        self.unblock_btn.clicked.connect(self.unblock)
        action_layout.addWidget(self.unblock_btn)

        self.time_btn = QPushButton("Set Time Limit (min)")
        self.time_btn.clicked.connect(self.set_time_limit)
        action_layout.addWidget(self.time_btn)

        clients_layout.addLayout(action_layout)
        main_layout.addWidget(clients_group)

        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Hotspot inactive")

    def start(self):
        try:
            ssid = self.ssid_entry.text()
            password = self.password_entry.text()
            max_clients = self.max_clients_spin.value()
            self.manager.set_config(ssid, password, max_clients)
            self.manager.start_hotspot()

            self.status_bar.showMessage("Hotspot active")
            self.start_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)

            # Start monitor
            self.monitor = MonitorThread(self.manager)
            self.monitor.new_device.connect(self.on_new_device)
            self.monitor.remove_device.connect(self.refresh_tree)
            self.monitor.update_list.connect(self.refresh_tree)
            self.monitor.error_occurred.connect(self.on_error)
            self.monitor.start()

        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def stop(self):
        if self.monitor:
            self.monitor.stop()
            self.monitor.wait(2000)
        self.manager.stop_hotspot()
        self.manager.cleanup_rules()
        self.status_bar.showMessage("Hotspot inactive")
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.tree.clear()

    def refresh_tree(self):
        self.tree.clear()
        for mac, dev in self.manager.connected_devices.items():
            status = "Approved" if dev['approved'] else ("Blocked" if mac in self.manager.blocked_devices else "Pending")
            expiry = dev['expiry'].strftime("%H:%M:%S") if dev['expiry'] else "Never"
            item = QTreeWidgetItem([mac, dev['ip'], dev['hostname'], status, expiry])
            self.tree.addTopLevelItem(item)

    def on_new_device(self, mac):
        self.refresh_tree()
        self.status_bar.showMessage(f"New device connected: {mac}")

    def on_error(self, err):
        self.status_bar.showMessage(f"Error: {err}")

    def get_selected_mac(self):
        item = self.tree.currentItem()
        if item:
            return item.text(0)
        return None

    def approve(self):
        mac = self.get_selected_mac()
        if mac:
            dev = self.manager.connected_devices.get(mac)
            if dev:
                self.manager.approve_device(mac, dev['ip'])
                self.refresh_tree()
                self.status_bar.showMessage(f"Approved: {mac}")

    def block_permanent(self):
        mac = self.get_selected_mac()
        if mac:
            dev = self.manager.connected_devices.get(mac)
            if dev:
                self.manager.block_device(mac, dev['ip'])
                self.refresh_tree()
                self.status_bar.showMessage(f"Blocked: {mac}")

    def unblock(self):
        mac = self.get_selected_mac()
        if mac:
            self.manager.unblock_permanently(mac)
            self.refresh_tree()
            self.status_bar.showMessage(f"Unblocked: {mac}")

    def set_time_limit(self):
        mac = self.get_selected_mac()
        if mac:
            minutes, ok = QInputDialog.getInt(self, "Time Limit", "Minutes until disconnect (0 = unlimited):", 60, 0, 1440)
            if ok:
                if minutes > 0:
                    self.manager.set_time_limit(mac, minutes)
                else:
                    self.manager.connected_devices[mac]['expiry'] = None
                self.refresh_tree()
                self.status_bar.showMessage(f"Time limit set for {mac}")

    def closeEvent(self, event):
        if self.monitor and self.monitor.isRunning():
            self.monitor.stop()
            self.monitor.wait(2000)
        if self.manager.running:
            self.manager.stop_hotspot()
            self.manager.cleanup_rules()
        event.accept()


# ---------------------- Entry Point ----------------------
def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')  # Looks good on Windows 7
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
