# from hardware.backend_factory import create_backend
# from hardware.device_loader import load_device

# backend = create_backend("grid", 4)
# device = load_device(backend)

# print(device)
# print(device.graph)

# from shell.qshell import QShell

# if __name__ == "__main__":
#     QShell().cmdloop()

from hardware.device_loader import load_device
from kernel.kernel import Kernel
from shell.qshell import QShell
from hardware.backend_factory import create_backend

device = load_device(create_backend("random", 10))
kernel = Kernel(device)
shell = QShell(kernel)
shell.cmdloop()