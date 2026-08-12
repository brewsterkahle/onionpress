# OnionPress Mission Review

Review the current task or change through the lens of OnionPress's three core author commitments:

## 1. Author Safety
- Does this change preserve or improve anonymity? Nothing should leak the author's real IP, identity, or location.
- Does it avoid creating new attack surfaces that could expose an author to retaliation, surveillance, or takedown?
- When in doubt, favor the approach that gives authors more control and less exposure.

## 2. Ease of Publishing
- Authors are not sysadmins. Any new complexity must be invisible by default or clearly optional.
- WordPress is the publishing surface by design — keep it central. Don't force authors to learn Tor internals, Docker, or CLI tooling to publish content.
- Error messages should guide, not dump. If something fails, tell the author what to do next, not what went wrong internally.

## 3. Content Durability
- Published content should outlast the author's machine, their ISP, or a hostile takedown attempt.
- Tor onion services provide resilience without DNS — no registrar can yank the domain. Preserve this property.
- Internet Archive integration is a durability backstop — archived content survives even if the onion service goes offline. Treat archival as a first-class feature, not an afterthought.

## Checklist
Before finalizing any design or implementation decision, confirm:
- [ ] Author anonymity is not weakened
- [ ] A non-technical author could use this without reading docs
- [ ] Content published through this path will still be reachable if the author's Mac is offline
- [ ] The change works without DNS (onion address only is valid)
- [ ] No new dependency on a centralized service that could be pressured to cut off access
