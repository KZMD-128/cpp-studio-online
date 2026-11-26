# Security notes

This service executes arbitrary C++ submitted by visitors. Treat every submission as hostile.

- Never run submitted C++ directly in the API process or API container.
- Each job is launched in a disposable container with no network, read-only root filesystem, bounded memory/CPU/PIDs, dropped capabilities except `SYS_PTRACE` for GDB, and a per-job writable directory.
- The API container has access to the Docker socket. This is a high-privilege capability; run it on a dedicated VPS and do not co-host sensitive workloads.
- CORS is not authentication. Add edge rate limiting and anti-abuse controls before public launch.
- Docker is not a perfect security boundary. Consider gVisor or per-job microVM isolation for an open public service.
- Keep Docker Engine, Linux kernel, runner image, and reverse proxy patched.
