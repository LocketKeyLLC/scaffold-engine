// Known-hit fixture (§17.1057): one warning, one guarded read.
const a = localStorage.getItem("k");                       // HIT
let b = null; try { b = localStorage.getItem("k"); } catch { b = null; }
