"""Windows Ethernet test-address setup; existing DHCP, DNS and routes are retained."""
import argparse
import base64
import ctypes as C
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

TEST_ADDRESS = "192.168.77.1"
TEST_PREFIX = 24


class Sockaddr4(C.Structure):
    _fields_ = [("family", C.c_uint16), ("port", C.c_uint16), ("address", C.c_uint8 * 4), ("zero", C.c_uint8 * 8)]


class SockaddrInet(C.Union):
    _fields_ = [("v4", Sockaddr4), ("storage", C.c_uint32 * 7)]


class UnicastRow(C.Structure):
    _fields_ = [("address", SockaddrInet), ("luid", C.c_uint64), ("index", C.c_uint32),
                ("prefix_origin", C.c_int32), ("suffix_origin", C.c_int32),
                ("valid_lifetime", C.c_uint32), ("preferred_lifetime", C.c_uint32),
                ("prefix", C.c_uint8), ("skip_source", C.c_uint8), ("dad", C.c_int32),
                ("scope", C.c_uint32), ("created", C.c_int64)]


def canonical_guid(value):
    return str(uuid.UUID(str(value).strip("{}")))


class WindowsAddressAPI:
    """Temporary secondary address through IP Helper, without DHCP configuration calls."""
    def __init__(self):
        if os.name != "nt": raise RuntimeError("Automatic adapter configuration requires Windows Python")
        self.dll = C.WinDLL("iphlpapi", use_last_error=True)
        for name in ("GetUnicastIpAddressEntry", "CreateUnicastIpAddressEntry", "DeleteUnicastIpAddressEntry"):
            function = getattr(self.dll, name)
            function.argtypes, function.restype = [C.POINTER(UnicastRow)], C.c_uint32
        self.dll.InitializeUnicastIpAddressEntry.argtypes = [C.POINTER(UnicastRow)]
        self.dll.InitializeUnicastIpAddressEntry.restype = None
        self.dll.ConvertInterfaceGuidToLuid.argtypes = [C.c_void_p, C.POINTER(C.c_uint64)]
        self.dll.ConvertInterfaceGuidToLuid.restype = C.c_uint32
        self.dll.ConvertInterfaceLuidToIndex.argtypes = [C.POINTER(C.c_uint64), C.POINTER(C.c_uint32)]
        self.dll.ConvertInterfaceLuidToIndex.restype = C.c_uint32

    @staticmethod
    def check(code):
        if code: raise OSError(code, C.FormatError(code))

    def row(self, guid, address=TEST_ADDRESS):
        raw = (C.c_uint8 * 16).from_buffer_copy(uuid.UUID(canonical_guid(guid)).bytes_le)
        luid, index = C.c_uint64(), C.c_uint32()
        self.check(self.dll.ConvertInterfaceGuidToLuid(raw, C.byref(luid)))
        self.check(self.dll.ConvertInterfaceLuidToIndex(C.byref(luid), C.byref(index)))
        row = UnicastRow()
        self.dll.InitializeUnicastIpAddressEntry(C.byref(row))
        row.address.v4.family = socket.AF_INET
        row.address.v4.address[:] = socket.inet_aton(address)
        row.luid, row.index = luid.value, index.value
        row.prefix_origin = row.suffix_origin = 1  # Manual; keep interface-level DHCP unchanged.
        row.prefix, row.skip_source = TEST_PREFIX, 1  # Explicit socket bind only, no automatic DNS/source use.
        return row

    def read(self, guid, address=TEST_ADDRESS):
        row = self.row(guid, address)
        code = self.dll.GetUnicastIpAddressEntry(C.byref(row))
        if code == 1168: return None
        self.check(code)
        return {"created": row.created, "prefix": row.prefix, "dad": row.dad,
                "skip_source": bool(row.skip_source), "index": row.index, "luid": row.luid}

    def create(self, guid):
        row = self.row(guid)
        self.check(self.dll.CreateUnicastIpAddressEntry(C.byref(row)))

    def delete(self, guid):
        row = self.row(guid)
        code = self.dll.DeleteUnicastIpAddressEntry(C.byref(row))
        if code != 1168: self.check(code)


def list_windows_adapters():
    if os.name != "nt": raise RuntimeError("Run the station under Windows Python for PC Ethernet configuration")
    script = r'''
$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new()
$rows=@(Get-NetAdapter | Where-Object {$_.HardwareInterface -and $_.InterfaceType -eq 6} | ForEach-Object {
 $a=$_; $i=Get-NetIPInterface -InterfaceIndex $a.ifIndex -AddressFamily IPv4 -ErrorAction Stop
 $ips=@(Get-NetIPAddress -InterfaceIndex $a.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | ForEach-Object {
  @{address=$_.IPAddress; prefix=[int]$_.PrefixLength; state=[string]$_.AddressState}
 })
 $routes=@(Get-NetRoute -InterfaceIndex $a.ifIndex -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | ForEach-Object {$_.NextHop})
 $dns=@((Get-DnsClientServerAddress -InterfaceIndex $a.ifIndex -AddressFamily IPv4).ServerAddresses)
 @{guid=[string]$a.InterfaceGuid; index=[int]$a.ifIndex; name=[string]$a.Name; description=[string]$a.InterfaceDescription;
   status=[string]$a.Status; mac=[string]$a.MacAddress; dhcp=[string]$i.Dhcp; ipv4=$ips; gateways=$routes; dns=$dns}
})
ConvertTo-Json -InputObject $rows -Depth 6 -Compress
'''
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                            capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError("Cannot read Ethernet adapters: " + result.stderr.decode("utf-8-sig", errors="replace")[-1200:])
    rows = json.loads(result.stdout.decode("utf-8-sig"))
    if not isinstance(rows, list): rows = [rows] if rows else []
    for row in rows: row["guid"] = canonical_guid(row["guid"])
    return rows


def retained_configuration(before, after):
    original = {(row["address"], row["prefix"]) for row in before["ipv4"]}
    current = {(row["address"], row["prefix"]) for row in after["ipv4"]}
    return original.issubset(current) and all(before[key] == after[key] for key in ("dhcp", "dns", "gateways"))


class NetworkSettings:
    def __init__(self, state_dir=None, api=None, enumerate_adapters=None):
        self.state_dir = Path(state_dir) if state_dir else Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PixNovaTestStation" / "network"
        self.api = api
        self.enumerate = enumerate_adapters or list_windows_adapters

    def adapters(self):
        return self.enumerate()

    def _api(self):
        if self.api is None: self.api = WindowsAddressAPI()
        return self.api

    def journal_path(self, guid):
        return self.state_dir / (canonical_guid(guid) + ".json")

    @contextmanager
    def mutation_lock(self, guid):
        """Serialize the complete address/journal transaction across stations and helpers."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.journal_path(guid).with_suffix(".lock").open("a+b") as guard:
            if os.name == "nt":
                import msvcrt
                guard.seek(0, os.SEEK_END)
                if guard.tell() == 0:
                    guard.write(b"\0")
                    guard.flush()
                guard.seek(0)
            else:
                import fcntl
            try:
                if os.name == "nt": msvcrt.locking(guard.fileno(), msvcrt.LK_NBLCK, 1)
                else: fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK): raise
                raise RuntimeError("Another station is changing this Ethernet adapter; retry after it finishes") from error
            # Closing the handle releases the OS lock, including on exceptions.
            yield

    def journal(self, guid):
        path = self.journal_path(guid)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def save(self, guid, value):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.journal_path(guid)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    def selected(self, guid, check_conflict=True):
        guid = canonical_guid(guid)
        rows = self.adapters()
        adapter = next((row for row in rows if row["guid"] == guid), None)
        if adapter is None: raise ValueError("Selected Ethernet adapter is no longer present; refresh adapters")
        if check_conflict and any(row["guid"] != guid and any(ip["address"] == TEST_ADDRESS for ip in row["ipv4"]) for row in rows):
            raise ValueError("The test address is already assigned to another adapter; select that adapter or restore it first")
        return adapter

    def status(self, guid, adapter=None):
        adapter = adapter or self.selected(guid)
        address = self._api().read(guid)
        owned = self.journal(guid)
        correct = bool(address and address["prefix"] == TEST_PREFIX)
        ready = correct and address["dad"] == 4 and adapter["status"] == "Up"
        message = "Test IP ready" if ready else "Test IP assigned; waiting for cable/address verification" if correct else "Test IP not configured"
        return {"adapter": adapter, "address": address, "ready": ready,
                "owned": bool(owned.get("owned")), "message": message}

    def configure_local(self, guid):
        guid = canonical_guid(guid)
        with self.mutation_lock(guid):
            return self._configure_locked(guid)

    @staticmethod
    def owns_address(record, address):
        return bool(record.get("owned") and record.get("created") is not None and address
                    and address["created"] == record["created"] and address["prefix"] == TEST_PREFIX
                    and address["skip_source"])

    def delete_owned_address(self, guid, record):
        address = self._api().read(guid)
        if address:
            if not self.owns_address(record, address):
                raise ValueError("The address was changed outside this station or its ownership could not be verified; it was not removed")
            self._api().delete(guid)

    def _configure_locked(self, guid):
        before = self.selected(guid)
        existing = self._api().read(guid)
        if existing:
            if existing["prefix"] != TEST_PREFIX or existing["dad"] in (0, 2, 3):
                raise ValueError("Existing test address has an incorrect prefix or an IP conflict; original settings retained")
            return self.status(guid, before)
        baseline = {"schema": 1, "guid": guid, "address": TEST_ADDRESS, "prefix": TEST_PREFIX,
                    "before": before, "owned": False, "utc": datetime.now(timezone.utc).isoformat()}
        self.save(guid, baseline)
        created = False
        try:
            self._api().create(guid)
            created = True
            address = self._api().read(guid)
            if not address: raise RuntimeError("Windows did not report the newly created test address")
            baseline.update(owned=True, created=address["created"])
            self.save(guid, baseline)
            # Poll DAD while connected. An unplugged adapter remains configured
            # and can be checked again after the cable is attached.
            for _ in range(12):
                if address["dad"] != 1 or before["status"] != "Up": break
                time.sleep(.5)
                address = self._api().read(guid)
                if not address: raise RuntimeError("Test address disappeared during verification")
            if not self.owns_address(baseline, address):
                raise ValueError("Test address ownership changed during verification")
            if address["dad"] in (0, 2, 3): raise ValueError("Windows detected an IP conflict")
            after = self.selected(guid)
            if not retained_configuration(before, after): raise RuntimeError("Network configuration changed during setup")
            return self.status(guid, after)
        except Exception as error:
            if created:
                try:
                    self.delete_owned_address(guid, baseline)
                except ValueError as ownership_error:
                    baseline["owned"] = False
                    self.save(guid, baseline)
                    raise ValueError(f"{error}; {ownership_error}") from error
                baseline["owned"] = False
                self.save(guid, baseline)
            raise

    def restore_local(self, guid):
        guid = canonical_guid(guid)
        with self.mutation_lock(guid):
            return self._restore_locked(guid)

    def _restore_locked(self, guid):
        adapter = self.selected(guid, check_conflict=False)
        record = self.journal(guid)
        if not record.get("owned"):
            return {**self.status(guid, adapter), "message": "No application-created test address to remove; original settings retained"}
        self.delete_owned_address(guid, record)
        record.update(owned=False, restored_utc=datetime.now(timezone.utc).isoformat())
        self.save(guid, record)
        return {**self.status(guid, self.selected(guid, check_conflict=False)), "message": "Test address removed; original DHCP, IP, gateway and DNS retained"}

    def ready_index(self, guid):
        address = self._api().read(canonical_guid(guid))
        if not address or address["prefix"] != TEST_PREFIX or address["dad"] != 4:
            raise ValueError("Configure the selected Ethernet adapter before testing")
        return address["index"]

    def change(self, action, guid):
        if action not in ("configure", "restore"): raise ValueError("Invalid network operation")
        guid = canonical_guid(guid)
        if os.name != "nt": raise RuntimeError("Automatic Ethernet configuration requires Windows Python")
        if action == "restore" and not self.journal(guid).get("owned"):
            return self.restore_local(guid)
        if action == "configure" and self._api().read(guid) is not None:
            return self.configure_local(guid)
        if C.windll.shell32.IsUserAnAdmin():
            return self.configure_local(guid) if action == "configure" else self.restore_local(guid)
        return elevated_change(action, guid, self.state_dir)


def elevated_change(action, guid, state_dir):
    """Use the standard Windows permission prompt; the GUI remains unprivileged."""
    from ctypes import wintypes as W
    class ExecuteInfo(C.Structure):
        _fields_ = [("size", W.DWORD), ("mask", W.ULONG), ("window", W.HWND), ("verb", W.LPCWSTR),
                    ("file", W.LPCWSTR), ("parameters", W.LPCWSTR), ("directory", W.LPCWSTR), ("show", C.c_int),
                    ("instance", W.HINSTANCE), ("idlist", C.c_void_p), ("class_name", W.LPCWSTR),
                    ("class_key", W.HKEY), ("hotkey", W.DWORD), ("icon", W.HANDLE), ("process", W.HANDLE)]
    shell, kernel = C.WinDLL("shell32", use_last_error=True), C.WinDLL("kernel32", use_last_error=True)
    shell.ShellExecuteExW.argtypes, shell.ShellExecuteExW.restype = [C.POINTER(ExecuteInfo)], W.BOOL
    kernel.WaitForSingleObject.argtypes, kernel.WaitForSingleObject.restype = [W.HANDLE, W.DWORD], W.DWORD
    kernel.CloseHandle.argtypes = [W.HANDLE]
    with tempfile.TemporaryDirectory(prefix="pixnova-network-") as folder:
        result_path = Path(folder) / "result.json"
        args = [str(Path(__file__).resolve()), "--action", action, "--guid", guid, "--state-dir", str(state_dir), "--result", str(result_path)]
        info = ExecuteInfo()
        info.size, info.mask, info.verb, info.file = C.sizeof(info), 0x40, "runas", sys.executable
        info.parameters, info.directory, info.show = subprocess.list2cmdline(args), tempfile.gettempdir(), 0
        if not shell.ShellExecuteExW(C.byref(info)):
            code = C.get_last_error()
            raise RuntimeError("Windows permission was cancelled; network settings retained" if code == 1223 else C.FormatError(code))
        try:
            while True:
                result = kernel.WaitForSingleObject(info.process, 500)
                if result == 0: break
                if result != 258: raise OSError(C.get_last_error(), "Cannot wait for the network helper")
        finally:
            kernel.CloseHandle(info.process)
        if not result_path.exists(): raise RuntimeError("Administrator helper did not return a result; refresh adapter status")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if not result["ok"]: raise RuntimeError(result["error"])
        return result["value"]


class NetworkWorker:
    def __init__(self, backend):
        self.backend, self.events, self.busy, self.action = backend, queue.Queue(), False, None

    def request(self, action, guid=None):
        if self.busy: return False
        self.busy, self.action = True, action
        def run():
            try:
                value = self.backend.adapters() if action == "refresh" else self.backend.status(guid) if action == "status" else self.backend.change(action, guid)
                self.events.put({"action": action, "guid": guid, "value": value})
            except Exception as error:
                self.events.put({"action": action, "guid": guid, "error": str(error)})
        threading.Thread(target=run, name="pixnova-network", daemon=True).start()
        return True

    def poll(self):
        try: event = self.events.get_nowait()
        except queue.Empty: return None
        self.busy = False
        return event


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("configure", "restore"), required=True)
    parser.add_argument("--guid", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    try:
        settings = NetworkSettings(args.state_dir)
        value = settings.configure_local(args.guid) if args.action == "configure" else settings.restore_local(args.guid)
        response = {"ok": True, "value": value}
    except Exception as error:
        response = {"ok": False, "error": str(error)}
    temporary = args.result.with_suffix(".tmp")
    temporary.write_text(json.dumps(response), encoding="utf-8")
    os.replace(temporary, args.result)
