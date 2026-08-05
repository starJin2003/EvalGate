## ❌ eval-gate — `grounded-docs-qa`

**Overall 0.931 → 0.973 (+0.042)**

| Category | Baseline | Candidate | Delta |
|---|---:|---:|---:|
| adversarial | 1.000 | 0.969 | -0.031 ⚠️ |
| comparison | 0.856 | 0.938 | +0.081 |
| factual | 0.969 | 1.000 | +0.031 |
| howto | 0.900 | 0.987 | +0.087 |

### Threshold breaches

- **adversarial** — dropped 0.031, limit 0.010

<details><summary>Regressed cases (3)</summary>

**`674d2d5c2b6956a4`** (adversarial) 1.00 → 0.25
> refusal: answered, refusal required; answered about absent symbol 'PydanticORMMapper'

**`992f8ccdbb1e841f`** (comparison) 1.00 → 0.75
> refusal: refused, answer required

**`d99b53caabf191d4`** (howto) 1.00 → 0.97
> citation: cited 18/19 factual sentences


</details>
