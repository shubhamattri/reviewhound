def charge(gateway, amount)
  gateway.charge(amount)
rescue StandardError
  nil
end
