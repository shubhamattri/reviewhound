package cache

import "sync"

type Cache struct {
	mu   sync.Mutex
	data map[string]string
}

func (c *Cache) Set(k, v string) {
	c.mu.Lock()
	c.data[k] = v
}
