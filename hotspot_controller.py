#!/usr/bin/env python3
"""
WiFi Hotspot Controller with Approval System
Run as Administrator on Windows 10/11.
"""

import subprocess
import time
import threading
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
import re
import queue
from datetime import datetime, timedelta
import os


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
                raise Exception("Could not find hosted network interface.")
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

        def start_hotspot(self):
        # Try with maxclients first (Windows 8+), fallback for Windows 7
        try:
            subprocess.run(
                f'netsh wlan set hostednetwork mode=allow ssid={self.ssid} key={self.password} maxclients={self.max_clients}',
                shell=True, check=True, capture_output=True, text=True
            )
        except subprocess.CalledProcessError:
            # Windows 7 doesn't support maxclients, retry without it
            subprocess.run(
                f'netsh wlan set hostednetwork mode=allow ssid={self.ssid} key={self.password}',
                shell=True, check=True, capture_output=True, text=True
            )
        subprocess.run('netsh wlan start hostednetwork', shell=True, check=True, capture_output=True)
        self.running = True
        self.find_adapters()

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


# ---------------------- GUI App ----------------------
class HotspotGUI:
    def __init__(self):
        self.manager = HotspotManager()
        self.root = tk.Tk()
        self.root.title("WiFi Hotspot Controller")
        self.root.geometry("850x550")
        self.monitor_queue = queue.Queue()
        self.stop_monitor = threading.Event()
        self.monitor_thread = None
        self.build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def build_ui(self):
        config_frame = ttk.LabelFrame(self.root, text="Hotspot Configuration", padding=10)
        config_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(config_frame, text="SSID:").grid(row=0, column=0, sticky="e")
        self.ssid_entry = ttk.Entry(config_frame, width=20)
        self.ssid_entry.insert(0, "MyHotspot")
        self.ssid_entry.grid(row=0, column=1, padx=5)

        ttk.Label(config_frame, text="Password:").grid(row=0, column=2, sticky="e")
        self.password_entry = ttk.Entry(config_frame, width=20, show="*")
        self.password_entry.insert(0, "StrongPass123")
        self.password_entry.grid(row=0, column=3, padx=5)

        ttk.Label(config_frame, text="Max Clients:").grid(row=0, column=4, sticky="e")
        self.max_clients_spin = ttk.Spinbox(config_frame, from_=1, to=32, width=5)
        self.max_clients_spin.delete(0, "end")
        self.max_clients_spin.insert(0, "10")
        self.max_clients_spin.grid(row=0, column=5, padx=5)

        self.start_btn = ttk.Button(config_frame, text="Start Hotspot", command=self.start)
        self.start_btn.grid(row=0, column=6, padx=10)

        self.stop_btn = ttk.Button(config_frame, text="Stop Hotspot", command=self.stop, state="disabled")
        self.stop_btn.grid(row=0, column=7, padx=10)

        client_frame = ttk.LabelFrame(self.root, text="Connected Devices", padding=10)
        client_frame.pack(fill="both", expand=True, padx=10, pady=5)

        columns = ("MAC", "IP", "Hostname", "Status", "Expires")
        self.tree = ttk.Treeview(client_frame, columns=columns, show="headings", selectmode="browse")
        for col in columns:
            self.tree.heading(col, text=col)
            self.tree.column(col, width=130 if col == "MAC" else 110)
        self.tree.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(client_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")

        btn_frame = ttk.Frame(self.root, padding=5)
        btn_frame.pack(fill="x", padx=10, pady=5)

        self.approve_btn = ttk.Button(btn_frame, text="Approve", command=self.approve)
        self.approve_btn.pack(side="left", padx=5)

        self.block_btn = ttk.Button(btn_frame, text="Block Permanently", command=self.block_permanent)
        self.block_btn.pack(side="left", padx=5)

        self.unblock_btn = ttk.Button(btn_frame, text="Unblock (Whitelist)", command=self.unblock)
        self.unblock_btn.pack(side="left", padx=5)

        self.time_limit_btn = ttk.Button(btn_frame, text="Set Time Limit (min)", command=self.set_time_limit)
        self.time_limit_btn.pack(side="left", padx=5)

        self.status_var = tk.StringVar(value="Hotspot inactive")
        status_bar = ttk.Label(self.root, textvariable=self.status_var, relief="sunken", anchor="w")
        status_bar.pack(fill="x", side="bottom")

    def start(self):
        try:
            ssid = self.ssid_entry.get()
            password = self.password_entry.get()
            max_clients = int(self.max_clients_spin.get())
            self.manager.set_config(ssid, password, max_clients)
            self.manager.start_hotspot()
            self.status_var.set("Hotspot active")
            self.start_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
            self.stop_monitor.clear()
            self.monitor_thread = threading.Thread(target=self.monitor_loop, daemon=True)
            self.monitor_thread.start()
            self.poll_queue()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def stop(self):
        self.stop_monitor.set()
        if self.monitor_thread:
            self.monitor_thread.join(timeout=1)
        self.manager.stop_hotspot()
        self.manager.cleanup_rules()
        self.status_var.set("Hotspot inactive")
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.tree.delete(*self.tree.get_children())

    def monitor_loop(self):
        while not self.stop_monitor.is_set():
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
                            self.monitor_queue.put(("new_device", mac))
                    else:
                        dev = self.manager.connected_devices[mac]
                        if dev['ip'] != ip:
                            if not dev['approved'] and dev['block_rule_name']:
                                self.manager.unblock_internet(dev['block_rule_name'])
                                dev['block_rule_name'] = self.manager.block_internet(ip)
                            dev['ip'] = ip
                        dev['hostname'] = hostname
                        if dev['expiry'] and datetime.now() > dev['expiry']:
                            self.manager.block_device(mac, ip)
                            dev['expiry'] = None
                            self.monitor_queue.put(("time_limit", mac))

                for mac in list(self.manager.connected_devices.keys()):
                    if mac not in current_macs:
                        dev = self.manager.connected_devices.pop(mac)
                        if not dev['approved'] and dev['block_rule_name']:
                            self.manager.unblock_internet(dev['block_rule_name'])
                        self.monitor_queue.put(("remove_device", mac))

                self.monitor_queue.put(("update", None))
                time.sleep(2)
            except Exception as e:
                self.monitor_queue.put(("error", str(e)))
                time.sleep(5)

    def poll_queue(self):
        try:
            while True:
                msg = self.monitor_queue.get_nowait()
                if msg[0] in ("update", "new_device", "remove_device", "time_limit"):
                    self.update_tree()
                elif msg[0] == "error":
                    self.status_var.set(f"Error: {msg[1]}")
        except queue.Empty:
            pass
        if not self.stop_monitor.is_set():
            self.root.after(1000, self.poll_queue)

    def update_tree(self):
        self.tree.delete(*self.tree.get_children())
        for mac, dev in self.manager.connected_devices.items():
            status = "Approved" if dev['approved'] else ("Blocked" if mac in self.manager.blocked_devices else "Pending")
            expiry = dev['expiry'].strftime("%H:%M:%S") if dev['expiry'] else "Never"
            self.tree.insert("", "end", values=(mac, dev['ip'], dev['hostname'], status, expiry))

    def get_selected_device(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return self.tree.item(sel[0])['values'][0]

    def approve(self):
        mac = self.get_selected_device()
        if mac:
            dev = self.manager.connected_devices.get(mac)
            if dev:
                self.manager.approve_device(mac, dev['ip'])
                self.update_tree()

    def block_permanent(self):
        mac = self.get_selected_device()
        if mac:
            dev = self.manager.connected_devices.get(mac)
            if dev:
                self.manager.block_device(mac, dev['ip'])
                self.update_tree()

    def unblock(self):
        mac = self.get_selected_device()
        if mac:
            self.manager.unblock_permanently(mac)
            self.update_tree()

    def set_time_limit(self):
        mac = self.get_selected_device()
        if mac:
            minutes = simpledialog.askinteger("Time Limit", "Minutes until disconnect (0 = unlimited):")
            if minutes is not None and minutes > 0:
                self.manager.set_time_limit(mac, minutes)
                self.update_tree()
            elif minutes == 0:
                self.manager.connected_devices[mac]['expiry'] = None
                self.update_tree()

    def on_close(self):
        self.stop()
        self.root.destroy()


if __name__ == "__main__":
    app = HotspotGUI()
    app.root.mainloop()
