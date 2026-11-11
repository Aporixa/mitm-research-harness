# Ethics and publication

1. **Disclosure first.** If a run shows a security-relevant behavior that
   the vendor has not been told about, report it through the vendor's
   channel before publishing anything. Wait for a fix or the agreed
   embargo. This applies to every lab equally; a cross-lab result goes to
   each affected vendor. The repository is public for transparency, so a
   finding that needs disclosure is not pushed until the vendor has fixed
   it or 90 days have passed since the report, whichever comes first. The
   harness and experiment designs are public; unreported findings are not.
2. **Human review gates every claim.** Detector labels are never published
   as findings. A person reads each flagged transcript and labels it.
3. **Human review gates every file.** `mrh export --reviewed` states
   that a person has read every file in the scrubbed directory. The scrub
   gate is a backstop, not a substitute.
4. **Version claims precisely.** A finding states the agent version, the
   models observed, the observation window and n. It does not generalize
   past them.
5. **Compare fairly.** Cross-lab results say which settings each agent ran
   under (permission mode, sandbox, approval mode) and do not rank labs on
   conditions that were not comparable.
6. **Report negatives.** If a retest does not reproduce a past finding,
   that result is published too, with the same detail.
7. **No third-party targets.** Experiments do not direct the agent at
   systems the operator does not own or have permission to test.
