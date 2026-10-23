package counter

import "sync"

type Counter struct {
	mu sync.Mutex
	n  map[string]int
}

func (c *Counter) Get(k string) int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.n[k]
}
