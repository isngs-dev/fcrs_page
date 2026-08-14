/**
 * Country calling codes for the schedule-booking phone field (`ScheduleCta`).
 * A static table, not a package dependency -- the widget ships as one
 * self-contained bundle (CLAUDE.md tech stack: "self-contained bundle") with
 * no npm phone-input library, so this is intentionally hand-maintained data
 * rather than a `libphonenumber`-style import. Sorted by country name.
 *
 * `iso` is the ISO 3166-1 alpha-2 code, used only to guess a sensible default
 * from the visitor's browser locale (`Intl.Locale.region`) -- never sent to
 * the backend. `dial` is the leading `+`-prefixed calling code prepended to
 * whatever the visitor types in the phone field.
 */
export interface CountryCallingCode {
  iso: string;
  name: string;
  dial: string;
}

export const COUNTRY_CALLING_CODES: readonly CountryCallingCode[] = [
  { iso: "AF", name: "Afghanistan", dial: "+93" },
  { iso: "AL", name: "Albania", dial: "+355" },
  { iso: "DZ", name: "Algeria", dial: "+213" },
  { iso: "AR", name: "Argentina", dial: "+54" },
  { iso: "AM", name: "Armenia", dial: "+374" },
  { iso: "AU", name: "Australia", dial: "+61" },
  { iso: "AT", name: "Austria", dial: "+43" },
  { iso: "AZ", name: "Azerbaijan", dial: "+994" },
  { iso: "BH", name: "Bahrain", dial: "+973" },
  { iso: "BD", name: "Bangladesh", dial: "+880" },
  { iso: "BY", name: "Belarus", dial: "+375" },
  { iso: "BE", name: "Belgium", dial: "+32" },
  { iso: "BO", name: "Bolivia", dial: "+591" },
  { iso: "BA", name: "Bosnia and Herzegovina", dial: "+387" },
  { iso: "BR", name: "Brazil", dial: "+55" },
  { iso: "BG", name: "Bulgaria", dial: "+359" },
  { iso: "KH", name: "Cambodia", dial: "+855" },
  { iso: "CM", name: "Cameroon", dial: "+237" },
  { iso: "CA", name: "Canada", dial: "+1" },
  { iso: "CL", name: "Chile", dial: "+56" },
  { iso: "CN", name: "China", dial: "+86" },
  { iso: "CO", name: "Colombia", dial: "+57" },
  { iso: "CR", name: "Costa Rica", dial: "+506" },
  { iso: "HR", name: "Croatia", dial: "+385" },
  { iso: "CU", name: "Cuba", dial: "+53" },
  { iso: "CY", name: "Cyprus", dial: "+357" },
  { iso: "CZ", name: "Czechia", dial: "+420" },
  { iso: "DK", name: "Denmark", dial: "+45" },
  { iso: "DO", name: "Dominican Republic", dial: "+1" },
  { iso: "EC", name: "Ecuador", dial: "+593" },
  { iso: "EG", name: "Egypt", dial: "+20" },
  { iso: "SV", name: "El Salvador", dial: "+503" },
  { iso: "EE", name: "Estonia", dial: "+372" },
  { iso: "ET", name: "Ethiopia", dial: "+251" },
  { iso: "FI", name: "Finland", dial: "+358" },
  { iso: "FR", name: "France", dial: "+33" },
  { iso: "GE", name: "Georgia", dial: "+995" },
  { iso: "DE", name: "Germany", dial: "+49" },
  { iso: "GH", name: "Ghana", dial: "+233" },
  { iso: "GR", name: "Greece", dial: "+30" },
  { iso: "GT", name: "Guatemala", dial: "+502" },
  { iso: "HN", name: "Honduras", dial: "+504" },
  { iso: "HK", name: "Hong Kong", dial: "+852" },
  { iso: "HU", name: "Hungary", dial: "+36" },
  { iso: "IS", name: "Iceland", dial: "+354" },
  { iso: "IN", name: "India", dial: "+91" },
  { iso: "ID", name: "Indonesia", dial: "+62" },
  { iso: "IR", name: "Iran", dial: "+98" },
  { iso: "IQ", name: "Iraq", dial: "+964" },
  { iso: "IE", name: "Ireland", dial: "+353" },
  { iso: "IL", name: "Israel", dial: "+972" },
  { iso: "IT", name: "Italy", dial: "+39" },
  { iso: "JM", name: "Jamaica", dial: "+1" },
  { iso: "JP", name: "Japan", dial: "+81" },
  { iso: "JO", name: "Jordan", dial: "+962" },
  { iso: "KZ", name: "Kazakhstan", dial: "+7" },
  { iso: "KE", name: "Kenya", dial: "+254" },
  { iso: "KW", name: "Kuwait", dial: "+965" },
  { iso: "LA", name: "Laos", dial: "+856" },
  { iso: "LV", name: "Latvia", dial: "+371" },
  { iso: "LB", name: "Lebanon", dial: "+961" },
  { iso: "LY", name: "Libya", dial: "+218" },
  { iso: "LT", name: "Lithuania", dial: "+370" },
  { iso: "LU", name: "Luxembourg", dial: "+352" },
  { iso: "MO", name: "Macao", dial: "+853" },
  { iso: "MY", name: "Malaysia", dial: "+60" },
  { iso: "MT", name: "Malta", dial: "+356" },
  { iso: "MX", name: "Mexico", dial: "+52" },
  { iso: "MD", name: "Moldova", dial: "+373" },
  { iso: "MC", name: "Monaco", dial: "+377" },
  { iso: "MN", name: "Mongolia", dial: "+976" },
  { iso: "ME", name: "Montenegro", dial: "+382" },
  { iso: "MA", name: "Morocco", dial: "+212" },
  { iso: "MM", name: "Myanmar", dial: "+95" },
  { iso: "NP", name: "Nepal", dial: "+977" },
  { iso: "NL", name: "Netherlands", dial: "+31" },
  { iso: "NZ", name: "New Zealand", dial: "+64" },
  { iso: "NI", name: "Nicaragua", dial: "+505" },
  { iso: "NG", name: "Nigeria", dial: "+234" },
  { iso: "MK", name: "North Macedonia", dial: "+389" },
  { iso: "NO", name: "Norway", dial: "+47" },
  { iso: "OM", name: "Oman", dial: "+968" },
  { iso: "PK", name: "Pakistan", dial: "+92" },
  { iso: "PA", name: "Panama", dial: "+507" },
  { iso: "PY", name: "Paraguay", dial: "+595" },
  { iso: "PE", name: "Peru", dial: "+51" },
  { iso: "PH", name: "Philippines", dial: "+63" },
  { iso: "PL", name: "Poland", dial: "+48" },
  { iso: "PT", name: "Portugal", dial: "+351" },
  { iso: "PR", name: "Puerto Rico", dial: "+1" },
  { iso: "QA", name: "Qatar", dial: "+974" },
  { iso: "RO", name: "Romania", dial: "+40" },
  { iso: "RU", name: "Russia", dial: "+7" },
  { iso: "RW", name: "Rwanda", dial: "+250" },
  { iso: "SA", name: "Saudi Arabia", dial: "+966" },
  { iso: "RS", name: "Serbia", dial: "+381" },
  { iso: "SG", name: "Singapore", dial: "+65" },
  { iso: "SK", name: "Slovakia", dial: "+421" },
  { iso: "SI", name: "Slovenia", dial: "+386" },
  { iso: "ZA", name: "South Africa", dial: "+27" },
  { iso: "KR", name: "South Korea", dial: "+82" },
  { iso: "ES", name: "Spain", dial: "+34" },
  { iso: "LK", name: "Sri Lanka", dial: "+94" },
  { iso: "SE", name: "Sweden", dial: "+46" },
  { iso: "CH", name: "Switzerland", dial: "+41" },
  { iso: "TW", name: "Taiwan", dial: "+886" },
  { iso: "TZ", name: "Tanzania", dial: "+255" },
  { iso: "TH", name: "Thailand", dial: "+66" },
  { iso: "TT", name: "Trinidad and Tobago", dial: "+1" },
  { iso: "TN", name: "Tunisia", dial: "+216" },
  { iso: "TR", name: "Turkey", dial: "+90" },
  { iso: "UA", name: "Ukraine", dial: "+380" },
  { iso: "AE", name: "United Arab Emirates", dial: "+971" },
  { iso: "GB", name: "United Kingdom", dial: "+44" },
  { iso: "US", name: "United States", dial: "+1" },
  { iso: "UY", name: "Uruguay", dial: "+598" },
  { iso: "UZ", name: "Uzbekistan", dial: "+998" },
  { iso: "VE", name: "Venezuela", dial: "+58" },
  { iso: "VN", name: "Vietnam", dial: "+84" },
  { iso: "YE", name: "Yemen", dial: "+967" },
  { iso: "ZM", name: "Zambia", dial: "+260" },
  { iso: "ZW", name: "Zimbabwe", dial: "+263" },
];

const BY_ISO = new Map(COUNTRY_CALLING_CODES.map((c) => [c.iso, c]));

/** Default dial code used when the browser locale can't be resolved to a
 * country in the table above -- United States, matching this platform's
 * other hardcoded US-centric fallbacks (never a blank/invalid selection). */
export const DEFAULT_COUNTRY_ISO = "US";

/**
 * Best-effort guess at the visitor's country from `navigator.language`
 * (e.g. `"en-GB"` -> `"GB"`), mirroring this file's existing
 * `Intl.DateTimeFormat().resolvedOptions().timeZone` auto-detect pattern for
 * the timezone selector. Falls back to `DEFAULT_COUNTRY_ISO` when the locale
 * carries no region subtag or the region isn't in the table -- never throws,
 * never leaves the selector without a value.
 */
export function guessCountryIso(): string {
  try {
    const locale = new Intl.Locale(navigator.language);
    const region = locale.region;
    if (region && BY_ISO.has(region)) return region;
  } catch {
    // Intl.Locale unsupported or navigator.language unparsable -- fall through.
  }
  return DEFAULT_COUNTRY_ISO;
}

export function dialCodeForIso(iso: string): string {
  return BY_ISO.get(iso)?.dial ?? BY_ISO.get(DEFAULT_COUNTRY_ISO)!.dial;
}
