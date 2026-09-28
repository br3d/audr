import { PieChart, Pie, Cell, Tooltip, Legend, ResponsiveContainer } from 'recharts'
import type { AllocationItem } from '../api/client'

const COLORS = [
  '#2563eb', '#16a34a', '#dc2626', '#d97706', '#7c3aed',
  '#0891b2', '#059669', '#db2777', '#ea580c', '#65a30d',
]

interface Props {
  items: AllocationItem[]
}

export default function AllocationChart({ items }: Props) {
  if (items.length === 0) {
    return <p role="note">No priced holdings — allocation chart unavailable.</p>
  }

  // parseFloat is acceptable here: chart rendering does not require decimal precision
  const data = items.map((item) => ({
    name: item.symbol,
    value: parseFloat(item.value_usd),
  }))

  return (
    <div role="img" aria-label="Asset allocation pie chart">
      <ResponsiveContainer width="100%" height={300}>
        <PieChart>
          <Pie
            data={data}
            dataKey="value"
            nameKey="name"
            cx="50%"
            cy="50%"
            outerRadius={100}
          >
            {data.map((_entry, index) => (
              <Cell key={index} fill={COLORS[index % COLORS.length]} />
            ))}
          </Pie>
          <Tooltip formatter={(val) => [`$${Number(val).toFixed(2)}`, '']} />
          <Legend />
        </PieChart>
      </ResponsiveContainer>
    </div>
  )
}
