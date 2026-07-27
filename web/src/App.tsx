import { createClient } from './api/client'
import { ChatScreen } from './screens/ChatScreen'

// Статика раздаётся тем же svarog serve, поэтому базовый URL пустой.
const api = createClient({ baseUrl: '' })

export function App() {
  return <ChatScreen api={api} />
}
