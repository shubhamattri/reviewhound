package config

import "encoding/json"

type Config struct {
	Port int
}

func Default() Config {
	return Config{Port: 8080}
}

func Validate(port int) bool {
	return port > 0 && port < 65536
}
