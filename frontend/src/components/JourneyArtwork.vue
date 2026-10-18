<template>
  <div class="journey-art" aria-label="湖泊、街巷与美食串联的城市漫游插画" role="img">
    <svg viewBox="0 0 720 440" fill="none" aria-hidden="true">
      <defs>
        <linearGradient id="roam-sky" x1="360" y1="0" x2="360" y2="440" gradientUnits="userSpaceOnUse"><stop stop-color="#E8EEE5"/><stop offset="1" stop-color="#F4F0E4"/></linearGradient>
        <linearGradient id="roam-lake" x1="70" y1="210" x2="440" y2="400" gradientUnits="userSpaceOnUse"><stop stop-color="#B4CEC5"/><stop offset="1" stop-color="#DCE6D7"/></linearGradient>
        <pattern id="roam-grain" width="6" height="6" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r=".6" fill="#426151" opacity=".1"/></pattern>
      </defs>
      <rect width="720" height="440" rx="26" fill="url(#roam-sky)"/>
      <circle cx="572" cy="90" r="42" fill="#E7BD72"/>
      <path d="M0 193 75 149 133 169 234 95 339 164 414 139 516 200 625 145 720 188V440H0Z" fill="#CCD7C5"/>
      <path d="M0 222 107 184 189 212 308 168 411 209 502 183 617 228 720 197V440H0Z" fill="#A9BCA3"/>
      <path d="M0 262C122 209 177 275 271 235S455 216 494 263 640 245 720 278V440H0Z" fill="#E7E4CF"/>
      <path d="M0 274C80 241 136 286 216 258S358 229 429 263 469 359 372 392 132 407 0 437Z" fill="url(#roam-lake)"/>
      <g stroke="#EDF3E8" stroke-width="2" stroke-linecap="round" opacity=".8"><path d="M38 319h72m30-20h95m-66 52h89m-217 20h75m170-48h52m-35 48h70m-158 23h58"/></g>
      <path d="M301 440c-31-69 66-77 147-112s31-63 107-87 120 12 165-2" stroke="#FAF7EC" stroke-width="33"/>
      <path d="M301 440c-31-69 66-77 147-112s31-63 107-87 120 12 165-2" stroke="#BCB7A0" stroke-width="1" stroke-dasharray="4 7"/>
      <g transform="translate(235 115)"><path d="M-17 49h58V33h-58Z" fill="#617C65"/><path d="m-25 34 37-13 38 13Z" fill="#345B48"/><path d="M-11 22h45V10h-45Z" fill="#7B9475"/><path d="m-18 11 30-12 30 12Z" fill="#345B48"/><path d="M0 0h24v-10H0Z" fill="#7B9475"/><path d="m-8-10 20-13 21 13Z" fill="#345B48"/><path d="M12-23v-10" stroke="#345B48" stroke-width="2"/></g>
      <g transform="translate(503 216)"><rect x="3" y="-17" width="64" height="57" fill="#F7EDDA"/><path d="m-8-17 43-24 45 24Z" fill="#6B7561"/><rect x="16" y="8" width="14" height="32" rx="7" fill="#8B9B7D"/><rect x="40" y="-2" width="13" height="15" fill="#C3CDB6"/><path d="M7 1h55" stroke="#CCBFA2"/><rect x="59" y="7" width="53" height="48" fill="#E4D2AF"/><path d="m52 7 34-17 35 17Z" fill="#A87052"/><rect x="71" y="23" width="13" height="32" fill="#7B8C6D"/><rect x="91" y="20" width="12" height="12" fill="#B49B78"/></g>
      <g transform="translate(397 318)"><path d="M-58 8C-32-34 25-34 54 8" stroke="#D1C09D" stroke-width="17"/><path d="M-58 0C-32-42 25-42 54 0" stroke="#F3EBD6" stroke-width="8"/><path d="M-44-15v-17m18 4v-17m22 12v-15m23 21v-16m20 31v-16" stroke="#EDE2C7" stroke-width="4"/></g>
      <g fill="#426B51"><path d="M92 238v68m-29-23 29-57 29 57Z"/><path d="M139 245v57m-24-17 24-46 24 46Z"/><path d="M641 303v72m-30-21 30-63 31 63Z"/><path d="M681 319v61m-24-17 24-54 24 54Z"/></g>
      <g fill="#8DAA7F"><circle cx="472" cy="229" r="21"/><circle cx="456" cy="242" r="17"/><circle cx="483" cy="245" r="17"/><circle cx="188" cy="228" r="17"/><circle cx="175" cy="240" r="14"/></g>
      <g stroke="#5A7757" stroke-width="3"><path d="M472 244v35m-286-40v28"/></g>
      <path d="M148 310C210 280 268 342 360 301S478 287 546 265" stroke="#C9784C" stroke-width="2.5" stroke-dasharray="5 7" stroke-linecap="round"/>
      <g fill="#FFFAEF" stroke="#B86C46" stroke-width="2"><circle cx="148" cy="310" r="7"/><circle cx="360" cy="301" r="7"/><circle cx="546" cy="265" r="7"/></g>
      <path d="m230 350 30-7-12 20-3-10Z" fill="#345B48"/>
      <path d="M481 89c6-7 12-7 18 0m0 0c6-7 12-7 18 0" stroke="#84967B" stroke-width="2" stroke-linecap="round"/>
      <rect width="720" height="440" rx="26" fill="url(#roam-grain)"/>
    </svg>
    <div class="art-caption"><span>一座城，慢慢认识。</span><span>WANDER · TASTE · STAY</span></div>
    <div class="place-note lake-note"><span class="note-number">01</span><span>沿着湖边走走<small>A little time to wander</small></span></div>
    <div class="place-note food-note"><span class="note-number food-number">02</span><span>下一站，吃点好的<small>Follow your appetite</small></span></div>
  </div>
</template>

<style scoped>
.journey-art {
  position: relative;
  isolation: isolate;
  padding: 0 0 30px;
}

.journey-art>svg {
  display: block;
  width: 100%;
  height: auto;
  border-radius: 26px;
}

.art-caption {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  position: absolute;
  bottom: 0;
  left: 3px;
  right: 3px;
  font-size: 11px;
  color: #7b8272;
}

.art-caption span:last-child {
  font-size: 9px;
  letter-spacing: 2px;
}

.place-note {
  position: absolute;
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 14px 18px;
  background: #fffef8;
  border: 1px solid #e7e8db;
  border-radius: 12px;
  box-shadow: 0 12px 30px #304b3a0c;
  font-size: 13px;
  font-weight: 600;
  color: #314c3c;
  transform: rotate(-4deg);
}

.place-note small {
  display: block;
  font-size: 9px;
  font-weight: 400;
  letter-spacing: .4px;
  margin-top: 4px;
  color: #899184;
}

.note-number {
  display: grid;
  place-items: center;
  width: 31px;
  height: 31px;
  background: #edf2e7;
  border-radius: 50%;
  font-size: 11px;
  font-family: Georgia,serif;
}

.lake-note {
  top: 42%;
  left: -3%;
}

.food-note {
  right: -2%;
  bottom: 17%;
  transform: rotate(4deg);
}

.food-number {
  background: #faeddb;
  color: #ae704b;
}

@media (max-width:600px) {
  .place-note {
    padding: 10px 12px;
    font-size: 11px;
  }
  .place-note small {
    font-size: 8px;
  }
  .lake-note {
    left: 0;
  }
  .food-note {
    right: 0;
  }
  .art-caption {
    font-size: 10px;
  }
  .art-caption span:last-child {
    font-size: 7px;
    letter-spacing: 1px;
  }
}

@media (prefers-reduced-motion:no-preference) {
  .journey-art {
    animation: art-appear .7s ease both;
  }
  @keyframes art-appear {
    from {
      opacity: 0;
      transform: translateY(10px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }
}
</style>
