import AssistantPanel from '../components/AssistantPanel'

/**
 * AUD-436: the assistant is an interface prototype, not a feature.
 *
 * It used to sit in the main nav and answer arbitrary questions with canned
 * phrases, which read as a product pretending to work. It is now reachable
 * only by typing `#/assistant-prototype` — there is no nav entry and no
 * in-app link — and the page states up front that nothing here looks at the
 * portfolio. When a real backend answers (AUD-302), the route goes back into
 * the nav and this wrapper goes away.
 */
export default function AssistantPrototypePage() {
  return (
    <div data-testid="assistant-prototype-page">
      <div className="alert alert-warning" role="note">
        <strong>Interface prototype — not a working feature.</strong> This screen exists only
        to show the shape of the planned assistant. Replies are fixed sample text played back
        in order: nothing here reads your wallets, balances, or prices, and no answer is about
        your portfolio. It is intentionally absent from the navigation.
      </div>
      <AssistantPanel />
    </div>
  )
}
