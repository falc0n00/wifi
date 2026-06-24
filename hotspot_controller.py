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
import uuid
from datetime import datetime, timedelta
import socket
import os

# ---------------------- Hotspot engine ----------------------
class HotspotManager:
    def __init__(self):
        self.ssid = "MyHotspot"
        self.password = "StrongPass123"
        self.max_clients = 10
        self.connected_devices = {}   # MAC -> {ip, hostname, approved, block_rule_name, expiry, first_seen, rx, tx}
        self.blocked_devices = set()  # MAC addresses that are permanently blocked
        self.running = False
        self.interface_name = None   # virtual adapter name
        self.internet_adapter = None # name of adapter with internet

    def find_adapters(self):
        """Detect the hosted network adapter and the internet-connected adapter."""
        try:
            # Find hosted network adapter
            output = subprocess.check_output("netsh wlan show hostednetwork", shell=True, text=True)
            match = re.search(r"Interface name\s*:\s*(.+)", output)
            if match:
                self.interface_name = match.group(1).strip()
            else:
                raise Exception("Could not find hosted network interface. Is your Wi-Fi adapter supported?")

            # Find the adapter with default route (internet)
            route_output = subprocess.check_output("route print 0.0.0.0", shell=True, text=True)
            # Extract the adapter name from the route table
            lines = route_output.splitlines()
            # Look for the adapter index first, then map to name
            # Simpler: use netsh interface ip show interfaces
            interfaces = subprocess.check_output("netsh interface ip show interfaces", shell=True, text=True)
            # We'll find the one that is "connected" and has a default gateway.
            # For simplicity, we'll ask user to select, but we can automate:
            for line in interfaces.splitlines():
                if "connected" in line.lower() and "dedicated" not in line.lower():
                    # line format:  Idx   Met   MTU   State        Name
                    # We'll parse later, but let's just ask user to pick
                    pass
            # Automated approach: use ipconfig to find default gateway
            ipconfig = subprocess.check_output("ipconfig", shell=True, text=True)
            adapter_sections = ipconfig.split("\r\n\r\n")
            for sec in adapter_sections:
                if "Default Gateway" in sec and " 0.0.0.0" not in sec:
                    # Extract the adapter name from the first line
                    adapter_line = sec.splitlines()[0].strip()
                    if adapter_line.endswith(':'):
                        adapter_name = adapter_line[:-1]
                    else:
                        adapter_name = adapter_line
                    if adapter_name != self.interface_name:
                        self.internet_adapter = adapter_name
                        return
            # Fallback: prompt
            raise Exception("Could not auto-detect internet adapter. Please set up sharing manually.")
        except subprocess.CalledProcessError as e:
            raise Exception(f"Failed to run netsh. Are you running as Administrator? {e}")

    def enable_ics(self):
        """Enable Internet Connection Sharing on the internet adapter for the hosted network."""
        # We'll use PowerShell to enable ICS using the Win32_NetworkAdapter class
        ps_script = f'''
        $public = Get-NetAdapter -Name "{self.internet_adapter}"
        $private = Get-NetAdapter -Name "{self.interface_name}"
        if (!$public -or !$private) {{
            Write-Error "Adapters not found"
            return
        }}
        # Enable sharing using the WMI method
        $mpublic = Get-WmiObject -Class Win32_NetworkAdapter -Filter "NetConnectionID='{self.internet_adapter}'"
        $mprivate = Get-WmiObject -Class Win32_NetworkAdapter -Filter "NetConnectionID='{self.interface_name}'"
        if ($mpublic -and $mprivate) {{
            $mpublic.EnableStatic(0, "192.168.137.1", "255.255.255.0")  # dummy, we don't need to set IP
            $sharing = $mpublic.EnableSharing(0)
        }} else {{
            # Fallback using HNetCfg
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
        """Configure and start the hosted network."""
        # Set SSID, password, and max clients
        subprocess.run(f'netsh wlan set hostednetwork mode=allow ssid={self.ssid} key={self.password} maxclients={self.max_clients}',
                       shell=True, check=True, capture_output=True)
        # Start
        subprocess.run('netsh wlan start hostednetwork', shell=True, check=True, capture_output=True)
        self.running = True
        self.find_adapters()
        try:
            self.enable_ics()
        except Exception as e:
            messagebox.showwarning("ICS Warning", f"Could not enable sharing automatically.\nPlease manually enable Internet Connection Sharing on '{self.internet_adapter}' for '{self.interface_name}'.\n\n{e}")

    def stop_hotspot(self):
        """Stop the hosted network."""
        subprocess.run('netsh wlan stop hostednetwork', shell=True, capture_output=True)
        self.running = False

    def set_config(self, ssid, password, max_clients):
        self.ssid = ssid
        self.password = password
        self.max_clients = max_clients
        if self.running:
            # Re-apply configuration and restart
            self.stop_hotspot()
            time.sleep(2)
            self.start_hotspot()

    def get_connected_clients(self):
        """Return list of (MAC, IP, Hostname) of currently associated clients."""
        try:
            output = subprocess.check_output('netsh wlan show hostednetwork', shell=True, text=True)
        except subprocess.CalledProcessError:
            return []
        # Find the "Clients" section
        client_section = re.findall(r'Clients connected.*?\n(.*?)\n\n', output, re.DOTALL)
        if not client_section:
            return []
        lines = client_section[0].strip().splitlines()
        clients = []
        for line in lines:
            # each line: MAC  IP  Hostname (sometimes)
            parts = line.strip().split(None, 2)
            if len(parts) >= 2:
                mac = parts[0].upper()
                ip = parts[1]
                hostname = parts[2] if len(parts) > 2 else "Unknown"
                clients.append((mac, ip, hostname))
        return clients

    def block_internet(self, ip_address):
        """Add a firewall rule to block outbound traffic for a specific IP."""
        rule_name = f"HotspotBlock_{ip_address}"
        subprocess.run(
            f'netsh advfirewall firewall add rule name="{rule_name}" dir=out remoteip={ip_address} protocol=any action=block',
            shell=True, capture_output=True
        )
        return rule_name

    def unblock_internet(self, rule_name):
        """Remove the blocking rule."""
        subprocess.run(f'netsh advfirewall firewall delete rule name="{rule_name}"', shell=True, capture_output=True)

    def approve_device(self, mac, ip):
        """Approve a device by removing the block rule and updating its status."""
        dev = self.connected_devices.get(mac)
        if dev and not dev['approved']:
            # Remove block rule
            self.unblock_internet(dev['block_rule_name'])
            dev['approved'] = True
            dev['block_rule_name'] = None
            return True
        return False

    def block_device(self, mac, ip):
        """Permanently block a device and revoke its access immediately."""
        dev = self.connected_devices.get(mac)
        if dev:
            if dev['approved']:
                # Re-block by adding rule
                rule_name = f"HotspotBlock_{ip}"
                self.block_internet(ip)
                dev['block_rule_name'] = rule_name
            dev['approved'] = False
            self.blocked_devices.add(mac)
        else:
            # If not even seen yet, just add to blocklist
            self.blocked_devices.add(mac)

    def unblock_permanently(self, mac):
        self.blocked_devices.discard(mac)
        # If currently connected and blocked, approve them
        if mac in self.connected_devices:
            dev = self.connected_devices[mac]
            if not dev['approved'] and dev.get('block_rule_name'):
                self.approve_device(mac, dev['ip'])

    def set_time_limit(self, mac, minutes):
        dev = self.connected_devices.get(mac)
        if dev:
            dev['expiry'] = datetime.now() + timedelta(minutes=minutes)

    def cleanup_rules(self):
        """Delete all firewall rules that start with 'HotspotBlock_'"""
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
        self.root.geometry("800x600")
        self.monitor_queue = queue.Queue()
        self.stop_monitor = threading.Event()
        self.monitor_thread = None
        self.build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def build_ui(self):
        # Top frame for configuration
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

        # Connected clients frame
        client_frame = ttk.LabelFrame(self.root, text="Connected Devices", padding=10)
        client_frame.pack(fill="both", expand=True, padx=10, pady=5)

        # Treeview
        columns = ("MAC", "IP", "Hostname", "Status", "Expires", "Traffic")
        self.tree = ttk.Treeview(client_frame, columns=columns, show="headings", selectmode="browse")
        for col in columns:
            self.tree.heading(col, text=col)
            self.tree.column(col, width=120 if col != "MAC" else 140)
        self.tree.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(client_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")

        # Buttons for selected device
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

        # Status bar
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
            # Start monitoring thread
            self.stop_monitor.clear()
            self.monitor_thread = threading.Thread(target=self.monitor_loop, daemon=True)
            self.monitor_thread.start()
            # Periodically update GUI from queue
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
        """Background thread that scans for new clients and traffic stats."""
        old_clients = set()
        while not self.stop_monitor.is_set():
            try:
                current_clients = self.manager.get_connected_clients()
                current_macs = set()
                for mac, ip, hostname in current_clients:
                    current_macs.add(mac)
                    if mac not in self.manager.connected_devices:
                        # New device – block its internet automatically
                        if mac not in self.manager.blocked_devices:
                            rule_name = self.manager.block_internet(ip)
                            self.manager.connected_devices[mac] = {
                                'ip': ip,
                                'hostname': hostname,
                                'approved': False,
                                'block_rule_name': rule_name,
                                'expiry': None,
                                'first_seen': datetime.now(),
                                'rx': 0,
                                'tx': 0
                            }
                            self.monitor_queue.put(("new_device", mac))
                    else:
                        # Update IP if changed (DHCP renewal)
                        dev = self.manager.connected_devices[mac]
                        if dev['ip'] != ip:
                            # If blocked, need to update firewall rule
                            if not dev['approved'] and dev['block_rule_name']:
                                self.manager.unblock_internet(dev['block_rule_name'])
                                dev['block_rule_name'] = self.manager.block_internet(ip)
                            dev['ip'] = ip
                        dev['hostname'] = hostname
                # Check for disconnected devices
                for mac in list(self.manager.connected_devices.keys()):
                    if mac not in current_macs:
                        dev = self.manager.connected_devices.pop(mac)
                        if not dev['approved'] and dev['block_rule_name']:
                            self.manager.unblock_internet(dev['block_rule_name'])
                        self.monitor_queue.put(("remove_device", mac))
                # Optional: traffic stats (we'll skip exact counters for brevity, could be added via psutil)
                self.monitor_queue.put(("update", None))
                time.sleep(2)
            except Exception as e:
                self.monitor_queue.put(("error", str(e)))
                time.sleep(5)

    def poll_queue(self):
        """Process messages from the monitoring thread and update the tree."""
        try:
            while True:
                msg = self.monitor_queue.get_nowait()
                if msg[0] == "update":
                    self.update_tree()
                elif msg[0] == "new_device":
                    self.update_tree()
                elif msg[0] == "remove_device":
                    self.update_tree()
                elif msg[0] == "error":
                    self.status_var.set(f"Error: {msg[1]}")
        except queue.Empty:
            pass
        if not self.stop_monitor.is_set():
            self.root.after(1000, self.poll_queue)

    def update_tree(self):
        """Refresh the treeview with current connected devices."""
        self.tree.delete(*self.tree.get_children())
        for mac, dev in self.manager.connected_devices.items():
            status = "Approved" if dev['approved'] else ("Blocked" if mac in self.manager.blocked_devices else "Pending")
            expiry = dev['expiry'].strftime("%H:%M:%S") if dev['expiry'] else "Never"
            traffic = f"{dev['rx']}/{dev['tx']}"  # placeholder
            self.tree.insert("", "end", values=(mac, dev['ip'], dev['hostname'], status, expiry, traffic))

    def get_selected_device(self):
        sel = self.tree.selection()
        if not sel:
            return None
        item = self.tree.item(sel[0])
        mac = item['values'][0]
        return mac

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
            minutes = simpledialog.askinteger("Time Limit", "Minutes until auto-disconnect (0 = unlimited):")
            if minutes is not None and minutes > 0:
                self.manager.set_time_limit(mac, minutes)
                # Schedule automatic disconnection (we'll implement a simple check in monitor_loop)
                self.update_tree()
            elif minutes == 0:
                self.manager.connected_devices[mac]['expiry'] = None
                self.update_tree()

    def on_close(self):
        self.stop()
        self.root.destroy()

# Add time-based disconnection inside monitor_loop (quick patch)
def enhanced_monitor_loop(self):
    # This will be integrated into the class; for brevity, I'll illustrate the logic
    pass

# Monkey-patch to add time limit enforcement in the existing monitor_loop
original_monitor_loop = HotspotGUI.monitor_loop
def patched_monitor_loop(self):
    # We'll copy the original code and add a check for expiry
    old_clients = set()
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
                            'rx': 0,
                            'tx': 0
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
                    # TIME LIMIT CHECK
                    if dev['expiry'] and datetime.now() > dev['expiry']:
                        # Disconnect this device
                        self.manager.block_device(mac, ip)  # blocks and removes approval
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

HotspotGUI.monitor_loop = patched_monitor_loop

if __name__ == "__main__":
    app = HotspotGUI()
    app.root.mainloop()