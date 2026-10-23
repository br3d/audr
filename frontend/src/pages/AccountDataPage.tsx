import PasswordSection from '../components/accountData/PasswordSection'
import ExportSection from '../components/accountData/ExportSection'
import PurgeSection from '../components/accountData/PurgeSection'

export default function AccountDataPage() {
  return (
    <div style={{ display: 'grid', gap: 20 }}>
      <p className="page-subheading">
        Manage your password, export your data, and purge provider records.
      </p>
      <PasswordSection />
      <ExportSection />
      <PurgeSection />
    </div>
  )
}
