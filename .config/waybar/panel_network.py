"""Saved Ethernet profiles and their available NetworkManager devices."""

NM = "org.freedesktop.NetworkManager"
NMP = "/org/freedesktop/NetworkManager"
ETHERNET = "802-3-ethernet"


class WiredConnections:
    def __init__(self, interface, properties):
        self.interface = interface
        self.properties = properties

    def snapshot(self):
        profiles = []
        settings = self.interface(NMP + "/Settings", NM + ".Settings")
        for path in settings.ListConnections():
            config = self.interface(path, NM + ".Settings.Connection").GetSettings()
            connection = config.get("connection", {})
            if connection.get("type") == ETHERNET:
                profiles.append((str(path), str(connection["id"])))

        devices = []
        for path in self.interface().GetDevices():
            device = self.properties(path, NM + ".Device")
            if int(device["DeviceType"]) != 1:
                continue
            wired = self.properties(path, NM + ".Device.Wired")
            active_path = str(device.get("ActiveConnection", "/"))
            active = (self.properties(active_path, NM + ".Connection.Active")
                      if active_path != "/" else {})
            devices.append({
                "path": str(path), "name": str(device["Interface"]),
                "state": int(device["State"]), "managed": bool(device.get("Managed", True)),
                "carrier": bool(wired["Carrier"]),
                "available": {str(p) for p in device.get("AvailableConnections", [])},
                "connection": str(active.get("Connection", "/")),
                "active_path": active_path, "ip4": str(device.get("Ip4Config", "/")),
            })

        rows = []
        for path, name in profiles:
            current = [d for d in devices if d["connection"] == path]
            available = [d for d in devices if path in d["available"]
                         and d["managed"] and d["carrier"] and d["state"] >= 30]
            # One saved profile can be active on more than one Ethernet adapter.
            for device in current or available[:1] or [None]:
                selected = device is not None and device["connection"] == path
                active = bool(selected and device["state"] == 100)
                busy = bool(selected and 40 <= device["state"] <= 110 and not active)
                status = "Conectar"
                if active:
                    addresses = []
                    if device["ip4"] != "/":
                        ip4 = self.properties(device["ip4"], NM + ".IP4Config")
                        addresses = [str(a["address"]) for a in ip4.get("AddressData", [])]
                    status = "Conectado · " + (", ".join(addresses) or device["name"])
                elif busy:
                    status = "Desconectando…" if device["state"] == 110 else "Conectando…"
                elif selected and device["state"] == 120:
                    status = "Falló la conexión · Reintentar"
                elif not devices:
                    status = "Sin adaptador Ethernet"
                elif not any(d["managed"] for d in devices):
                    status = "Adaptador no administrado · Abrí Ajustes"
                elif not any(d["carrier"] for d in devices if d["managed"]):
                    status = "Conectá el cable Ethernet"
                elif device is None:
                    status = "Sin adaptador compatible · Abrí Ajustes"
                rows.append({
                    "path": path, "name": name,
                    "device": device["path"] if device else None,
                    "interface": device["name"] if device else "",
                    "active_path": device["active_path"] if selected else "/",
                    "active": active, "busy": busy, "status": status,
                    "can_connect": bool(device in available and not busy and not active),
                })
        rows.sort(key=lambda r: (not r["active"], r["name"].casefold() != "controlador",
                                 r["name"].casefold(), r["interface"]))
        if not devices:
            status = "No se encontró un adaptador Ethernet."
        elif not any(d["carrier"] for d in devices):
            status = "Sin cable conectado."
        else:
            status = "Cable conectado · Elegí un perfil."
        return rows, status

    def connect_command(self, item):
        """Validate the target and preserve Internet routing for CONTROLADOR."""
        config = self.interface(item["path"], NM + ".Settings.Connection").GetSettings()
        connection = config.get("connection", {})
        device = self.properties(item["device"], NM + ".Device")
        if connection.get("type") != ETHERNET or int(device["DeviceType"]) != 1:
            raise RuntimeError("El perfil y el adaptador deben ser Ethernet.")
        if item["path"] not in device.get("AvailableConnections", []):
            raise RuntimeError("El perfil ya no está disponible. Revisá el cable y Actualizá.")
        if str(connection["id"]).casefold() != "controlador":
            return []
        if all(config.get(family, {}).get("never-default", False) for family in ("ipv4", "ipv6")):
            return []
        # nmcli changes only these properties, preserving addresses and saved secrets.
        return ["nmcli", "--wait", "15", "connection", "modify", "uuid", str(connection["uuid"]),
                "ipv4.never-default", "yes", "ipv6.never-default", "yes"]
