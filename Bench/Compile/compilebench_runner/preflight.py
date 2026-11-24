"""Check ordinary-user build dependencies before one compilation."""
from pathlib import Path
import subprocess
import tempfile


def check_environment(cfg):
    checks = []
    for name in ("build_root", "results_dir"):
        path = Path(cfg[name])
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=path):
            pass
        checks.append({"check": name, "path": str(path), "writable": True})
    text = Path(cfg["kernel_config"]).read_text()
    probes = [("C toolchain", "#include <stdio.h>\nint main(void) { return 0; }\n", [], "gcc/binutils 和 C 开发库")]
    if any(f"{symbol}=y" in text for symbol in
           ("CONFIG_STACK_VALIDATION", "CONFIG_UNWINDER_ORC", "CONFIG_DEBUG_INFO_BTF", "CONFIG_OBJTOOL")):
        probes.append(("libelf", "#include <libelf.h>\nint main(void) { return !elf_version(EV_CURRENT); }\n",
                       ["-lelf"], "elfutils-libelf-devel"))
    if any(f"{symbol}=y" in text for symbol in
           ("CONFIG_MODULE_SIG", "CONFIG_SYSTEM_TRUSTED_KEYRING", "CONFIG_SYSTEM_REVOCATION_LIST")):
        probes.append(("OpenSSL", "#include <openssl/evp.h>\nint main(void) { return EVP_sha256() == 0; }\n",
                       ["-lcrypto"], "openssl-devel"))
    with tempfile.TemporaryDirectory(prefix="compiletest-preflight-", dir=cfg["build_root"]) as directory:
        for name, code, libraries, package in probes:
            command = [*cfg["compiler"], "-x", "c", "-", "-o", str(Path(directory) / "probe"), *libraries]
            result = subprocess.run(command, input=code, text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, timeout=30)
            if result.returncode != 0:
                raise ValueError(f"{name} 编译/链接预检失败；Fedora 请检查 {package}\n{result.stdout.strip()}")
            checks.append({"check": name, "command": command, "exit_code": 0})
    return checks
