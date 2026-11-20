import AssistantPanel from '../components/AssistantPanel'

/** AUD-956: the "AI assistant" tab (replaces the AUD-436 prototype page). */
export default function AssistantPage() {
  return (
    <div data-testid="assistant-page" className="assistant-page">
      <AssistantPanel />
    </div>
  )
}
