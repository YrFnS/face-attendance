import base64
import gzip
from pathlib import Path

root = Path(__file__).resolve().parent
parts = sorted((root / ".p2").glob("worker_apply.*.b64"))
if [part.name for part in parts] != [
    f"worker_apply.{index:02d}.b64" for index in range(6)
]:
    raise SystemExit("P2-03 staging payload is incomplete")
payload = "".join(part.read_text(encoding="ascii") for part in parts)
source = gzip.decompress(base64.b64decode(payload, validate=True))
exec(compile(source, "p2_apply_delivery_worker_impl.py", "exec"))
fix = root / ".p2" / "worker_apply_fix.py"
exec(compile(fix.read_bytes(), str(fix), "exec"))
