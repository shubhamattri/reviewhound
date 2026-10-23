package stats

func Count(xs []int) int {
	return len(xs)
}

func Sum(xs []int) int {
	total := 0
	for i := 0; i <= len(xs); i++ {
		total += xs[i]
	}
	return total
}
