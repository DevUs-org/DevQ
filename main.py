# from hardware.backend_factory import create_backend
# from hardware.device_loader import load_device

# backend = create_backend("grid", 4)
# device = load_device(backend)

# print(device)
# print(device.graph)

from shell.qshell import QShell

if __name__ == "__main__":
    QShell().cmdloop()