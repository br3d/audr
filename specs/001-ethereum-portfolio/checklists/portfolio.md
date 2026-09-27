# Portfolio Requirements Review Checklist

**Purpose**: Review the quality of release-1 portfolio, coverage, access, and operating requirements before implementation.
**Created**: 2026-09-26
**Feature**: [Ethereum Portfolio](../spec.md)
**Depth**: Standard
**Audience / timing**: Specification author and reviewer, before implementation

## Requirement Completeness

- [x] CHK001 Are the supported native ETH and ERC-20 balance types, and the limits of catalog-based discovery, stated without implying universal token discovery? [Completeness, Spec §FR-006–FR-008]
- [x] CHK002 Are catalog maintenance and update expectations specified sufficiently to define what “maintained” means for release 1? [Gap, Spec §FR-007]
- [x] CHK003 Are the required fields and scope of the owner-facing data export specified, including how observations, quotes, coverage, and exclusions are represented? [Gap, Spec §FR-020]
- [x] CHK004 Are the consequences of removing a wallet or excluding an asset specified for current totals, future snapshots, and preserved historical snapshots? [Completeness, Spec §FR-005, §FR-014–FR-016]
- [x] CHK005 Are first-run setup, sign-out, password change, and prevention of a second owner all covered by explicit requirements? [Completeness, Spec §FR-001–FR-002]

## Requirement Clarity

- [x] CHK006 Is “supported ERC-20” defined well enough to distinguish a zero balance, an unreadable contract, missing metadata, and a contract outside discovery scope? [Clarity, Spec §FR-006–FR-010]
- [x] CHK007 Is the valuation basis clear when fresh balances, last-known balances, stale prices, and unpriced holdings coexist in one portfolio? [Clarity, Spec §FR-012–FR-013]
- [x] CHK008 Is “stale contribution” defined so a reviewer can tell whether it refers to asset quantities, their USD value, or both? [Clarity, Spec §FR-013]
- [x] CHK009 Are the rules for labeling a total “estimated” versus “incomplete” unambiguous when a wallet has never scanned successfully or an asset has no usable price? [Clarity, Spec §FR-013, §SC-003]
- [x] CHK010 Is the denominator and completeness label for asset and network allocation specified when only part of the portfolio has usable USD prices? [Clarity, Spec §FR-011–FR-013]
- [x] CHK011 Are observation time, block identity, quote time, last attempt, and last successful scan differentiated wherever freshness is displayed? [Clarity, Spec §FR-012–FR-013, §FR-018]
- [x] CHK012 Is the meaning of “history beginning with tracking” clear for the interval between adding a wallet and its first successful priced observation? [Clarity, Spec §FR-015–FR-016]

## Requirement Consistency

- [x] CHK013 Do the dashboard, historical snapshot, and export requirements agree on whether an estimated total includes last-known quantities after a partial refresh failure? [Consistency, Spec §FR-013, §FR-015, §FR-020]
- [x] CHK014 Do the asset identity and deduplication requirements consistently use chain plus contract address rather than symbol, including catalog/manual overlap? [Consistency, Spec §FR-007–FR-010]
- [x] CHK015 Do the freshness labels for balances and prices remain distinct across holdings, total valuation, allocation, and history requirements? [Consistency, Spec §FR-011–FR-016]
- [x] CHK016 Are ordinary restart persistence and web export clearly distinguished from the deferred full backup/restore capability? [Consistency, Spec §FR-020, §Assumptions]

## Acceptance Criteria Quality

- [x] CHK017 Can the exactness criterion be evaluated for quantities and displayed USD totals without assuming binary floating-point arithmetic or a hidden rounding rule? [Measurability, Spec §FR-010, §SC-002]
- [x] CHK018 Do the partial-failure criteria define observable outcomes for both a previously scanned wallet and a never-successfully-scanned wallet? [Measurability, Spec §FR-013, §SC-003]
- [x] CHK019 Does the performance criterion specify a reproducible dataset, measurement boundary, percentile, and reference environment without implying an external RPC speed guarantee? [Measurability, Spec §SC-004, §Assumptions]
- [x] CHK020 Are authentication, protected views, masked secrets, and credential-free exports independently assessable from the success criteria? [Measurability, Spec §FR-001–FR-003, §FR-020, §SC-007]

## Scenario and Edge Case Coverage

- [x] CHK021 Are zero ETH, nonzero tokens, very small quantities, large integers, and varying token decimals represented without conflating display rounding with stored quantity? [Coverage, Spec §Edge Cases, §FR-010]
- [x] CHK022 Is expected behavior specified when an ERC-20 call reverts, metadata is deceptive or absent, or only some catalog contracts can be read? [Coverage, Spec §Edge Cases, §FR-008–FR-009]
- [x] CHK023 Are empty portfolios, excluded-only portfolios, and portfolios with unpriced holdings covered by meaningful total/allocation states? [Coverage, Spec §FR-011–FR-014]
- [x] CHK024 Are gaps, portfolio membership changes, exclusion changes, and corrected chain reorganizations covered without presenting them as investment profit or loss? [Coverage, Spec §FR-014–FR-016, §FR-019]
- [x] CHK025 Are interrupted, overlapping, retried, and rate-limited scans covered so their required effect on current balances and snapshots is unambiguous? [Coverage, Spec §FR-009, §FR-017–FR-019]
- [x] CHK026 Is concurrent first-time setup covered with an explicit single-owner outcome and no ambiguous partial setup state? [Coverage, Spec §FR-001, §Edge Cases]

## Non-Functional Requirements and Dependencies

- [x] CHK027 Are RPC and quote-provider data disclosures precise enough for an owner to understand which addresses, asset identifiers, and credentials leave the installation? [Clarity, Spec §FR-024]
- [x] CHK028 Are credential storage, failed-login protection, and access to exported portfolio data specified at a level that can be reviewed without assuming an unstated deployment boundary? [Gap, Spec §FR-002–FR-003, §FR-020, §FR-024]
- [x] CHK029 Are provider outages, rate limits, polling bounds, and stale-data thresholds assigned explicit owner-visible states or documented planning decisions? [Dependency, Spec §FR-017–FR-018, §Assumptions]
- [x] CHK030 Are release-1 exclusions explicit enough to prevent news, AI advice, notifications, activity monitoring, additional networks, and backup/restore from entering the portfolio task list? [Scope, Spec §Assumptions]

## Notes

- These questions assess the specification and its acceptance criteria, not implementation behavior. Mark items complete only after the requirements are reviewed or clarified.
- The existing general [requirements checklist](requirements.md) remains separate.

## Focused Follow-Up: Valuation, Scan Coverage, Access, Export, and MVP Scope

- [x] CHK031 Is the required order of quantity multiplication, aggregation, and USD rounding specified so wallet subtotals and the portfolio total cannot disagree solely because of rounding? [Clarity, Spec §FR-010–FR-013, §SC-002]
- [x] CHK032 Do the requirements distinguish a failed balance read for one wallet-asset pair from failure of the entire wallet scan, including the effect of each on the estimated total? [Coverage, Spec §FR-009–FR-010, §FR-013]
- [x] CHK033 Is partial catalog discovery defined in terms of completed, failed, and unattempted contracts so the coverage label cannot imply that an interrupted scan was exhaustive? [Clarity, Spec §FR-007, §FR-009]
- [x] CHK034 Are the threshold and reference time for labeling a balance stale specified, including a successful scan that returns the same quantity as before? [Gap, Spec §FR-013, §FR-018]
- [x] CHK035 Is the treatment of an unavailable or expired quote explicit when a last-known balance is still usable, including whether its USD contribution is unknown rather than zero? [Coverage, Spec §FR-012–FR-013]
- [x] CHK036 Are the criteria for recording a historical valuation point clear when only prices change, only some balances refresh, or no usable valuation exists? [Clarity, Spec §FR-015–FR-016]
- [x] CHK037 Are history requirements clear about whether points before and after wallet membership or exclusion changes are comparable, and how those scope changes are identified? [Clarity, Spec §FR-014–FR-016]
- [x] CHK038 Does the address access model state whether any public Ethereum address may be tracked without proof of ownership, while preserving the prohibition on keys, signatures, and transaction permissions? [Gap, Spec §FR-005, §FR-023]
- [x] CHK039 Are export requirements explicit about historical scope, machine-readable quantity precision, snapshot provenance, and protection of the resulting downloadable file? [Gap, Spec §FR-010, §FR-015, §FR-020, §FR-024]
- [x] CHK040 Are the boundaries between release-1 balance observation and deferred transaction/activity monitoring, DeFi accounting, news, AI, and notifications stated consistently in the spec and plan? [Consistency, Spec §FR-006, §FR-023, §Assumptions]

## Focused Follow-Up: Data Removal and Migration Recovery

- [x] CHK041 Are the effects of explicit provider-data removal on historical USD values, retained onchain observations, and existing owner exports specified consistently? [Coverage, Gap, Spec §FR-015, §FR-020]
- [x] CHK042 Are failure and recovery requirements for schema changes clear about committed-record preservation and service readiness without requiring full backup/restore? [Coverage, Gap, Spec §FR-020]

## Review Findings

- Reviewed against the specification, plan, data model, research, and contracts on 2026-09-26. The four remaining questions were resolved in the specification and supporting artifacts; all forty-two items now pass.
- CHK003 resolved: FR-020 and the HTTP export schema define current/history scope, record fields, exact numeric strings, statuses, coverage, revisions and exclusions.
- CHK023 resolved: FR-013 and the UI contract define zero included value and no chart for an excluded-only portfolio after successful observations; unknown included inputs retain a null total.
- CHK034 resolved: balance freshness uses verified block time, with read time separate; a later successful block refreshes unchanged quantities and retrying the same observation adds no history.
- CHK038 resolved: any valid public mainnet address may be tracked without proof of control and without an ownership claim.
- CHK041 is supported by the provider-purge contract and web UI contract: affected history becomes unpriced, onchain observations remain, and separately retained exports are outside removal scope.
- CHK042 is supported by the plan and quickstart: failed migrations leave API/worker unready without deleting committed data; full backup/restore remains deferred.
