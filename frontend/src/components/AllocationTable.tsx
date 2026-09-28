import type { AllocationItem } from '../api/client'
import MoneyValue from './MoneyValue'

interface Props {
  items: AllocationItem[]
}

export default function AllocationTable({ items }: Props) {
  if (items.length === 0) {
    return <p role="note">No allocation data available.</p>
  }

  return (
    <table aria-label="Asset allocation">
      <thead>
        <tr>
          <th scope="col">Asset</th>
          <th scope="col">Value (USD)</th>
          <th scope="col">Allocation</th>
        </tr>
      </thead>
      <tbody>
        {items.map((item) => (
          <tr key={item.asset_id}>
            <td>{item.symbol}</td>
            <td>
              <MoneyValue value={item.value_usd} />
            </td>
            <td>
              <span aria-label={`${item.percentage} percent`}>{item.percentage}%</span>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
