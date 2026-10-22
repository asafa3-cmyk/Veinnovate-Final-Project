"""Hash-verified, offline raw-window export for chapter figures; originals remain unchanged."""
import sys,json,hashlib
from pathlib import Path
import numpy as np
O=Path(__file__).resolve().parent;E=O.parent;R=E.parents[1]
sys.path[:0]=[str(E),str(R/'Hardware_integration_ready/receiver'),str(R/'VeinNet_integration_ready')]
import analyze_fast1 as A
run=Path.home()/'VeinnovateRuns/POC-20261021-FAST1'
W=A.build_windows(A.read_raw(run,'POC-20261021-FAST1'))
assert len(W)==18 and all(len(w['red'])==len(w['ir'])==3000 for w in W)
np.savez_compressed(O/'raw_windows.npz',**{w['slot']+'_'+c:w[c] for w in W for c in ['red','ir']})
for manifest,base in [(E/'SHA256SUMS_FAST1_RESULTS.txt',E/'results'),(E/'independent_audit/SHA256SUMS.txt',E/'independent_audit')]:
 for line in manifest.read_text().splitlines():
  h,n=line.split(maxsplit=1);assert hashlib.sha256((base/n).read_bytes()).hexdigest()==h,n
print('18 raw windows / 54,000 pairs exported; original result and independent-audit hashes match')
